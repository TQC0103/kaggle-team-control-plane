"""Credential references and isolated subprocess environments.

Only an environment-variable *name* is durable. Its value is resolved just in
time and is never returned by this module or written to SQLite.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

from .errors import ValidationError


ENV_REF_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KAGGLE_SECRET_ENV_KEYS = {
    "KAGGLE_API_TOKEN",
    "KAGGLE_KEY",
    "KAGGLE_USERNAME",
    "KAGGLE_CONFIG_DIR",
}
SAFE_CHILD_ENV_KEYS = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PYTHONIOENCODING",
}
ALLOWED_CREDENTIAL_KEYS = {
    "KAGGLE_API_TOKEN",
    "KAGGLE_KEY",
    "KAGGLE_USERNAME",
    "token",
    "key",
    "username",
}


def validate_env_ref(value: str) -> str:
    if not isinstance(value, str) or not ENV_REF_PATTERN.fullmatch(value):
        raise ValidationError(
            "credential_env_ref must be an environment-variable name, not a credential"
        )
    return value


class EnvCredentialVault:
    """Resolve Kaggle credentials from the service process environment."""

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = environ if environ is not None else os.environ

    def is_available(self, credential_env_ref: str | None) -> bool:
        return bool(credential_env_ref and self._environ.get(credential_env_ref))

    def credential_identity_hint(self, credential_env_ref: str) -> tuple[str | None, dict[str, str]]:
        """Return a legacy username hint and minimal Kaggle auth variables."""
        validate_env_ref(credential_env_ref)
        raw = self._environ.get(credential_env_ref)
        if not raw:
            raise ValidationError(
                f"credential environment variable {credential_env_ref!r} is not set"
            )
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if not isinstance(decoded, dict):
            return None, {"KAGGLE_API_TOKEN": raw}

        unexpected = set(decoded) - ALLOWED_CREDENTIAL_KEYS
        if unexpected:
            raise ValidationError(
                "credential JSON contains unsupported keys: "
                + ", ".join(sorted(unexpected))
            )
        aliases = {
            "token": "KAGGLE_API_TOKEN",
            "key": "KAGGLE_KEY",
            "username": "KAGGLE_USERNAME",
        }
        credentials = {
            aliases.get(str(key), str(key)): str(value)
            for key, value in decoded.items()
            if value is not None and str(value)
        }
        if not ({"KAGGLE_API_TOKEN", "KAGGLE_KEY"} & set(credentials)):
            raise ValidationError("credential reference does not contain a Kaggle token/key")
        return credentials.get("KAGGLE_USERNAME"), credentials

    def build_subprocess_env(
        self,
        credential_env_ref: str,
        kaggle_username: str,
        isolated_config_dir: str | Path,
    ) -> dict[str, str]:
        validate_env_ref(credential_env_ref)
        credential_username, credentials = self.credential_identity_hint(
            credential_env_ref
        )
        if credential_username and credential_username.casefold() != kaggle_username.casefold():
            raise ValidationError(
                "credential username does not match the explicitly assigned Kaggle account"
            )
        credentials["KAGGLE_USERNAME"] = kaggle_username

        # Use a small process-launch allowlist: an agent process often carries
        # unrelated API secrets that must never be inherited by Kaggle CLI.
        child_env = {
            str(key): str(value)
            for key, value in os.environ.items()
            if key.upper() in SAFE_CHILD_ENV_KEYS or key.upper().startswith("LC_")
        }
        child_env.update(credentials)
        child_env["KAGGLE_CONFIG_DIR"] = str(Path(isolated_config_dir).resolve())
        return child_env

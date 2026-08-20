"""In-memory Kaggle OAuth helpers for DPAPI-backed desktop credentials."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from kagglesdk import KaggleCredentials, KaggleEnv, KaggleOAuth
from kagglesdk.kaggle_client import KaggleClient


OAUTH_KIND = "kaggle_oauth_v1"
OAUTH_SCOPES = ["resources.admin:*"]


class InMemoryKaggleCredentials(KaggleCredentials):
    """Refresh OAuth credentials without writing Kaggle's plaintext file."""

    def save(self, file_path: str | None = None) -> None:
        del file_path


def parse_oauth_bundle(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("kind") != OAUTH_KIND:
        return None
    required = {"refresh_token", "username"}
    if not required <= set(value) or not all(str(value[key]) for key in required):
        raise ValueError("Stored Kaggle OAuth credential is incomplete")
    return value


def _bundle_from_credentials(credentials: KaggleCredentials) -> dict[str, Any]:
    expiration = credentials._access_token_expiration  # noqa: SLF001
    return {
        "kind": OAUTH_KIND,
        "refresh_token": credentials._refresh_token,  # noqa: SLF001
        "access_token": credentials._access_token or "",  # noqa: SLF001
        "access_token_expiration": expiration.isoformat() if expiration else "",
        "username": credentials.get_username() or "",
        "scopes": list(credentials._scopes or []),  # noqa: SLF001
    }


def serialize_oauth_bundle(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))


def run_browser_oauth() -> tuple[str, dict[str, Any]]:
    """Run Kaggle's official browser flow without persisting plaintext creds."""
    with KaggleClient(env=KaggleEnv.PROD) as client:
        oauth = KaggleOAuth(client=client)
        # Kaggle's public authenticate() always saves ~/.kaggle/credentials.json.
        # The underlying flow returns the same credential object before that
        # write, allowing Control Plane to encrypt it directly with DPAPI.
        credentials = oauth._run_oauth_flow(OAUTH_SCOPES, no_launch_browser=False)  # noqa: SLF001
        username = credentials.introspect()
        return username, _bundle_from_credentials(credentials)


def resolve_oauth_access_token(
    bundle: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return a valid access token and the possibly refreshed OAuth bundle."""
    expiration_text = str(bundle.get("access_token_expiration") or "")
    expiration = datetime.fromisoformat(expiration_text) if expiration_text else None
    with KaggleClient(env=KaggleEnv.PROD) as client:
        credentials = InMemoryKaggleCredentials(
            client=client,
            refresh_token=str(bundle["refresh_token"]),
            access_token=str(bundle.get("access_token") or ""),
            access_token_expiration=expiration,
            username=str(bundle["username"]),
            scopes=[str(scope) for scope in bundle.get("scopes") or OAUTH_SCOPES],
        )
        token = credentials.get_access_token()
        if not token:
            raise RuntimeError("Kaggle did not return an OAuth access token")
        return token, _bundle_from_credentials(credentials)

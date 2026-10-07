"""Bounded private checkpoint transfer; authentication stays in the desktop app."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from .adapters import KaggleCliAdapter
from .errors import ValidationError

_LOCK = threading.Lock()
_REF = re.compile(r"^[A-Za-z0-9_-]{1,50}/[A-Za-z0-9_-]{6,50}$")


def _account_cli(service, account_id):
    account = service.database.get_account(account_id)
    if account["state"] != "enabled" or not account.get("credential_env_ref"):
        raise ValidationError("an enabled authenticated account is required")
    if not isinstance(service.adapter, KaggleCliAdapter):
        raise ValidationError("dataset transfer requires the Kaggle CLI adapter")
    config_dir = service.data_dir / "dataset-transfer-config" / account_id
    config_dir.mkdir(parents=True, exist_ok=True)
    env = service.vault.build_subprocess_env(
        account["credential_env_ref"], account["kaggle_username"], config_dir
    )

    def run(args, timeout=120):
        try:
            output = service.adapter._run(args, env, threading.Event(), timeout_seconds=timeout)
        except Exception as exc:
            raise ValidationError(service.scheduler._redact(str(exc), env)[:1500]) from None
        return service.scheduler._redact(output, env)

    return account, run


def validate_bundle(service, payload):
    source = Path(payload.get("source_dir", "")).resolve()
    if not source.is_relative_to(service.allowed_source_root) or source == service.allowed_source_root:
        raise ValidationError("dataset source must be a dedicated directory in the allowed experiments root")
    if source.is_symlink() or not source.is_dir():
        raise ValidationError("dataset source must be a regular directory")
    files = list(source.iterdir())
    if {p.name for p in files} != {"dataset-metadata.json", "resume_latest.pth"}:
        raise ValidationError("transfer bundle must contain only metadata and resume_latest.pth")
    if any(p.is_symlink() or not p.is_file() for p in files):
        raise ValidationError("transfer files must be regular files")
    cp = source / "resume_latest.pth"
    if not 0 < cp.stat().st_size <= 512 * 1024 * 1024:
        raise ValidationError("checkpoint transfer is limited to 512 MiB")
    with cp.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != payload.get("checkpoint_sha256"):
        raise ValidationError("checkpoint SHA256 mismatch")
    meta = json.loads((source / "dataset-metadata.json").read_text(encoding="utf-8-sig"))
    if not _REF.fullmatch(meta.get("id", "")) or meta.get("isPrivate") is not True:
        raise ValidationError("a valid dataset id and explicit isPrivate=true are required")
    allowed = {"id", "title", "description", "licenses", "isPrivate", "collaborators"}
    if set(meta) - allowed:
        raise ValidationError("unsupported transfer metadata fields")
    for entry in meta.get("collaborators", []):
        if entry.get("role") != "READER" or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", entry.get("username", "")):
            raise ValidationError("only explicit reader collaborators are allowed")
    return source, meta, digest, cp.stat().st_size


def publish_private_checkpoint(service, payload, actor):
    service._reject_secrets(payload)
    source, meta, digest, size = validate_bundle(service, payload)
    account, run = _account_cli(service, payload.get("account_id"))
    if meta["id"].split("/")[0].casefold() != account["kaggle_username"].casefold():
        raise ValidationError("dataset owner must match the selected account")
    enabled_names = {a["kaggle_username"] for a in service.database.list_accounts() if a["state"] == "enabled"}
    if any(c["username"] not in enabled_names for c in meta.get("collaborators", [])):
        raise ValidationError("collaborators must be enabled control-plane accounts")
    # Serial creation plus the CLI's existing-dataset check makes an uncertain
    # retry safe: never creates a second version or changes checkpoint contents.
    with _LOCK:
        create_output = run(["datasets", "create", "-p", str(source), "-q"], 600)
        service.database.append_audit(actor, "dataset.private_checkpoint_create_response", "account", account["id"],
                                      {"dataset_ref": meta["id"], "cli_output": create_output[:2000]})
        if "Dataset creation error:" in create_output and "already in use" not in create_output:
            raise ValidationError(create_output[:2000])
        try:
            update_output = run(["datasets", "metadata", meta["id"], "--update", "-p", str(source)], 120)
        except ValidationError as exc:
            raise ValidationError(f"Create response: {create_output[:1200]}; metadata update: {exc}") from None
        verify_dir = service.artifact_root / "dataset-transfers" / digest / "owner-metadata"
        verify_dir.mkdir(parents=True, exist_ok=True)
        run(["datasets", "metadata", meta["id"], "-p", str(verify_dir)], 120)
        remote = json.loads((verify_dir / "dataset-metadata.json").read_text())
        remote = remote.get("info") or remote
        if remote.get("isPrivate") is not True:
            raise ValidationError("remote dataset private state was not confirmed")
        file_output = run(["datasets", "files", meta["id"], "--csv"], 120)
        if "resume_latest.pth" not in file_output:
            raise ValidationError("checkpoint file is not yet available on the dataset")
        result = {"dataset_ref": meta["id"], "is_private": True, "checkpoint_sha256": digest,
                  "checkpoint_bytes": size, "files": file_output, "create": create_output,
                  "metadata_update": update_output}
        service.database.append_audit(actor, "dataset.private_checkpoint_publish", "account", account["id"],
                                      {k: v for k, v in result.items() if k not in {"create", "files", "metadata_update"}})
        return result


def check_private_checkpoint_access(service, payload, actor):
    service._reject_secrets(payload)
    ref = payload.get("dataset_ref", "")
    if not _REF.fullmatch(ref):
        raise ValidationError("invalid dataset reference")
    account, run = _account_cli(service, payload.get("account_id"))
    output = run(["datasets", "files", ref, "--csv"], 120)
    if "resume_latest.pth" not in output:
        raise ValidationError("checkpoint read access was not confirmed")
    service.database.append_audit(actor, "dataset.private_checkpoint_access", "account", account["id"],
                                  {"dataset_ref": ref, "checkpoint_visible": True})
    return {"account_id": account["id"], "dataset_ref": ref, "checkpoint_visible": True, "files": output}


def private_checkpoint_status(service, payload, actor):
    service._reject_secrets(payload)
    ref = payload.get("dataset_ref", "")
    if not _REF.fullmatch(ref):
        raise ValidationError("invalid dataset reference")
    account, run = _account_cli(service, payload.get("account_id"))
    status = run(["datasets", "status", ref], 120)
    return {"account_id": account["id"], "dataset_ref": ref, "cli_status": status}

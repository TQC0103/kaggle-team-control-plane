"""Validated application service for the control-plane REST API."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import (
    DEFAULT_GPU_MACHINE_SHAPE,
    DEFAULT_MAX_SOURCE_BYTES,
    DEFAULT_TPU_MACHINE_SHAPE,
    FakeKaggleAdapter,
    KaggleAdapter,
    KaggleCliAdapter,
    hidden_subprocess_kwargs,
    normalize_kernel_slug,
)
from .credentials import EnvCredentialVault, validate_env_ref
from .database import Database
from .errors import ConflictError, ValidationError
from .scheduler import JobScheduler


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
SECRET_INPUT_FIELDS = {
    "token",
    "key",
    "secret",
    "password",
    "credential",
    "kaggle_key",
    "kaggle_api_token",
    "api_token",
    "access_token",
    "client_secret",
}
GPU_MACHINE_SHAPES = {DEFAULT_GPU_MACHINE_SHAPE}
TPU_MACHINE_SHAPES = {DEFAULT_TPU_MACHINE_SHAPE}


def _required_text(payload: dict[str, Any], name: str, max_length: int = 500) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{name} is too long")
    return value


def _nonnegative_number(value: Any, name: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValidationError(f"{name} must be a non-negative number")
    return float(value)


class ControlPlaneService:
    def __init__(
        self,
        database_path: str | Path,
        *,
        data_dir: str | Path | None = None,
        adapter: KaggleAdapter | None = None,
        adapter_name: str = "kaggle",
        vault: EnvCredentialVault | None = None,
        max_workers: int = 10,
        max_jobs_per_account: int = 2,
        remote_poll_seconds: float = 5.0,
        live_log_poll_seconds: float = 30.0,
        dispatch_poll_seconds: float = 0.1,
        allowed_source_root: str | Path | None = None,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
        quota_sync_seconds: float = 300.0,
        quota_start_delay_seconds: float = 0.0,
        start_scheduler: bool = True,
    ):
        self.database = Database(database_path)
        self.data_dir = Path(data_dir or Path(database_path).parent / "control-plane-data").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_root = self.data_dir / "artifacts"
        self.allowed_source_root = Path(
            allowed_source_root
            or os.environ.get("KCP_ALLOWED_SOURCE_ROOT")
            or Path.cwd()
        ).expanduser().resolve()
        self.vault = vault or EnvCredentialVault()
        if adapter is None:
            if adapter_name == "fake":
                adapter = FakeKaggleAdapter()
            elif adapter_name == "kaggle":
                adapter = KaggleCliAdapter()
            else:
                raise ValidationError("adapter_name must be fake or kaggle")
        self.adapter = adapter
        self._recovery_stop = threading.Event()
        # Recovery keeps checking a remotely active kernel after a desktop
        # restart.  Retain its last observed state in memory so a long Kaggle
        # queue does not flood the event history with identical polls.
        self._recovery_observations: dict[str, str] = {}
        self._recovery_observations_lock = threading.Lock()
        recovered_job_ids = self.database.recover_interrupted_jobs()
        self.recovered_jobs = len(recovered_job_ids)
        self.scheduler = JobScheduler(
            self.database,
            self.adapter,
            self.vault,
            self.data_dir,
            max_workers=max_workers,
            max_jobs_per_account=max_jobs_per_account,
            remote_poll_seconds=remote_poll_seconds,
            live_log_poll_seconds=live_log_poll_seconds,
            dispatch_poll_seconds=dispatch_poll_seconds,
            max_source_bytes=max_source_bytes,
            quota_sync_seconds=quota_sync_seconds,
            quota_start_delay_seconds=quota_start_delay_seconds,
        )
        if start_scheduler:
            self.scheduler.start()
            # A Kaggle CLI status command can be slow or temporarily blocked.
            # Never make the desktop API/UI wait for that remote round trip.
            # The preserved active state is already safe, and the scheduler
            # will not dispatch onto its account while it is unresolved.
            self._recovery_threads: list[threading.Thread] = []
            # Keep every recovery independent. A slow artifact download from
            # one completed kernel must not delay status reconciliation for a
            # different remote kernel.
            for job_id in recovered_job_ids:
                thread = threading.Thread(
                    target=self._reconcile_recovered_jobs,
                    args=([job_id],),
                    name=f"kcp-startup-remote-reconciliation-{job_id[-8:]}",
                    daemon=True,
                )
                thread.start()
                self._recovery_threads.append(thread)
        else:
            # Deterministic service construction is useful for the backend
            # suite and other non-desktop callers.
            self._recovery_threads = []
            self._reconcile_recovered_jobs(recovered_job_ids)

    def close(self) -> None:
        self._recovery_stop.set()
        self.scheduler.close()

    def _schedule_recovered_job_recheck(self, job_id: str) -> None:
        """Keep reconciling a remote run even if startup had no network.

        A desktop close/reopen must not require a user to manually press a
        retry button just to discover that Kaggle finished meanwhile.
        """
        def recheck() -> None:
            if not self._recovery_stop.wait(30.0):
                self._reconcile_recovered_jobs([job_id], announce_restart=False)

        threading.Thread(
            target=recheck,
            name=f"kcp-recovery-recheck-{job_id[-8:]}",
            daemon=True,
        ).start()

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "database": "ok",
            "adapter": self.adapter.__class__.__name__,
            "scheduler": self.scheduler.snapshot(),
            "recovered_jobs_on_start": self.recovered_jobs,
        }

    def _should_report_recovery_observation(
        self, job_id: str, observation: str, *, announce_restart: bool
    ) -> bool:
        """Return whether this recovery observation merits a user-facing event."""
        with self._recovery_observations_lock:
            previous = self._recovery_observations.get(job_id)
            self._recovery_observations[job_id] = observation
        return announce_restart or previous != observation

    def _reconcile_recovered_jobs(
        self, job_ids: list[str], *, announce_restart: bool = True
    ) -> None:
        """Refresh the true Kaggle state for jobs active when the app closed.

        This runs before the scheduler accepts new work, so an existing remote
        kernel never appears as a fabricated failure nor loses its account
        concurrency guard.  A temporary CLI/network error leaves the job
        active and marked uncertain; a later restart or explicit status query
        can safely retry the check.
        """
        for job_id in job_ids:
            env: dict[str, str] | None = None
            try:
                job = self.database.get_job(job_id)
                account = self.database.get_account(job["account_id"])
                credential_ref = account.get("credential_env_ref")
                if not credential_ref:
                    raise ValidationError("assigned account has no credential reference")
                config_dir = (self.scheduler.config_root / f"recovery-{job_id}").resolve()
                config_dir.mkdir(parents=True, exist_ok=True)
                env = self.vault.build_subprocess_env(
                    credential_ref, account["kaggle_username"], config_dir
                )
                remote = self.adapter.status(job, env, threading.Event())
                safe_detail = self.scheduler._redact(remote.detail, env)
                if self._should_report_recovery_observation(
                    job_id, remote.state, announce_restart=announce_restart
                ):
                    status_prefix = (
                        "Kaggle remote status after restart"
                        if announce_restart
                        else "Kaggle remote status changed"
                    )
                    self.database.append_job_event(
                        job_id,
                        f"{status_prefix}: {remote.state}",
                        details={"detail": safe_detail},
                        level="error" if remote.state == "failed" else "info",
                    )
                # A locally cancelled/failed job can still have
                # ``remote_may_be_running`` set.  Remote status is
                # authoritative during recovery, so include those stale local
                # terminal labels as valid transition sources as well.
                active_states = {
                    "submitting", "submitted", "running", "cancel_requested",
                    "succeeded", "failed", "cancelled",
                }
                if remote.state == "running":
                    self.database.transition_job(
                        job_id,
                        active_states,
                        "running",
                        fields={"error": None, "remote_may_be_running": 1},
                    )
                    self._schedule_recovered_job_recheck(job_id)
                elif remote.state == "complete":
                    # Remote status is authoritative. Persist it before
                    # downloading artifacts: an output transfer can be slow,
                    # but it must not make a completed Kaggle job look active.
                    self.database.transition_job(
                        job_id,
                        active_states,
                        "succeeded",
                        fields={
                            "error": None,
                            "remote_may_be_running": 0,
                            "result": {
                                "recovered_after_restart": True,
                                "remote_status": safe_detail,
                                "output_pending": True,
                            },
                        },
                    )
                    try:
                        output = self.scheduler._redact(
                            self.adapter.output(job, env, threading.Event()), env
                        )
                        self.database.transition_job(
                            job_id,
                            {"succeeded"},
                            "succeeded",
                            fields={
                                "result": {
                                    "recovered_after_restart": True,
                                    "remote_status": safe_detail,
                                    "output": output,
                                }
                            },
                        )
                    except Exception as exc:
                        self.database.append_job_event(
                            job_id,
                            "Could not download completed Kaggle output after restart",
                            details={"error": self.scheduler._redact(str(exc), env)[:2000]},
                            level="warning",
                        )
                    terminal_log_lines = 0
                    try:
                        terminal_log = self.adapter.terminal_logs(job, env, threading.Event())
                        terminal_log_lines = self.scheduler._replace_remote_log_text(
                            job_id, terminal_log, env
                        )
                        if terminal_log_lines:
                            self.database.append_job_event(
                                job_id,
                                "Reconciled complete Kaggle log after restart",
                                details={"line_count": terminal_log_lines},
                            )
                    except Exception as exc:
                        self.database.append_job_event(
                            job_id,
                            "Could not reconcile complete Kaggle log after restart",
                            details={"error": self.scheduler._redact(str(exc), env)[:2000]},
                            level="warning",
                        )
                    self.scheduler._schedule_terminal_log_rechecks(
                        job_id, job["kernel_slug"], env, terminal_log_lines
                    )
                elif remote.state == "failed":
                    self.database.transition_job(
                        job_id,
                        active_states,
                        "failed",
                        fields={
                            "error": safe_detail or "Kaggle reported failure",
                            "remote_may_be_running": 0,
                            "result": {
                                "recovered_after_restart": True,
                                "remote_status": safe_detail,
                                "diagnostics_pending": True,
                            },
                        },
                    )
                    terminal_log_lines = 0
                    try:
                        failure_output = self.scheduler._redact(
                            self.adapter.diagnostics(job, env, threading.Event()), env
                        )
                        self.scheduler._redact_downloaded_text_files(failure_output, env)
                        terminal_log_lines = self.scheduler._replace_remote_logs_from_download(
                            job_id, failure_output, env
                        )
                        self.database.transition_job(
                            job_id,
                            {"failed"},
                            "failed",
                            fields={
                                "result": {
                                    "recovered_after_restart": True,
                                    "remote_status": safe_detail,
                                    "failure_output": failure_output,
                                }
                            },
                        )
                        if terminal_log_lines:
                            self.database.append_job_event(
                                job_id,
                                "Reconciled failed Kaggle log after restart",
                                details={"line_count": terminal_log_lines},
                            )
                    except Exception as exc:
                        self.database.append_job_event(
                            job_id,
                            "Could not download failed Kaggle kernel diagnostics after restart",
                            details={"error": self.scheduler._redact(str(exc), env)[:2000]},
                            level="warning",
                        )
                    self.scheduler._schedule_terminal_log_rechecks(
                        job_id, job["kernel_slug"], env, terminal_log_lines
                    )
                elif remote.state == "cancelled":
                    self.database.transition_job(
                        job_id,
                        active_states,
                        "cancelled",
                        fields={
                            "error": safe_detail or "Kaggle reported cancellation",
                            "remote_may_be_running": 0,
                            "result": {
                                "recovered_after_restart": True,
                                "remote_status": safe_detail,
                            },
                        },
                    )
                # Kaggle may briefly report queued. ``submitted`` remains the
                # local representation of a successfully submitted kernel.
                elif remote.state == "queued":
                    self.database.transition_job(
                        job_id,
                        active_states,
                        "submitted",
                        fields={"error": None, "remote_may_be_running": 1},
                    )
                    self._schedule_recovered_job_recheck(job_id)
                else:
                    self.database.append_job_event(
                        job_id,
                        "Kaggle returned an unrecognised state after restart; keeping job active",
                        details={"remote_state": remote.state, "detail": safe_detail},
                        level="warning",
                    )
                    self._schedule_recovered_job_recheck(job_id)
            except Exception as exc:
                if self._should_report_recovery_observation(
                    job_id, "error", announce_restart=announce_restart
                ):
                    message = (
                        "Could not reconcile Kaggle state after restart; "
                        "keeping job active and retrying automatically"
                        if announce_restart
                        else "Could not reconcile Kaggle state; keeping job active and retrying automatically"
                    )
                    self.database.append_job_event(
                        job_id,
                        message,
                        details={"error": self.scheduler._redact(str(exc), env)[:2000]},
                        level="warning",
                    )
                self._schedule_recovered_job_recheck(job_id)

    @staticmethod
    def _decorate_job(job: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(job)
        metadata = decorated.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        accelerator = str(metadata.get("accelerator") or "cpu")
        decorated["accelerator"] = accelerator
        decorated["machine_shape"] = metadata.get("machine_shape")

        result = decorated.get("result")
        output = result.get("output") if isinstance(result, dict) else None
        runtime = output.get("runtime") if isinstance(output, dict) else None
        decorated["runtime"] = runtime if isinstance(runtime, dict) else None

        started_at = decorated.get("remote_started_at") or decorated.get("started_at")
        finished_at = decorated.get("finished_at")
        if isinstance(started_at, str):
            try:
                start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                end = (
                    datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                    if isinstance(finished_at, str)
                    else datetime.now(timezone.utc)
                )
                decorated["elapsed_seconds"] = max(0, int((end - start).total_seconds()))
            except ValueError:
                decorated["elapsed_seconds"] = None
        else:
            decorated["elapsed_seconds"] = None
        return decorated

    @classmethod
    def _decorate_batch(cls, batch: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(batch)
        jobs = decorated.get("jobs")
        if isinstance(jobs, list):
            decorated["jobs"] = [
                cls._decorate_job(job) if isinstance(job, dict) else job for job in jobs
            ]
        return decorated

    @staticmethod
    def _reject_secrets(payload: dict[str, Any]) -> None:
        bad = {key for key in payload if key.casefold() in SECRET_INPUT_FIELDS}
        if bad:
            raise ValidationError(
                "plaintext credential fields are forbidden; use credential_env_ref"
            )

    @staticmethod
    def _reject_nested_secrets(value: Any, location: str = "metadata") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in SECRET_INPUT_FIELDS:
                    raise ValidationError(
                        f"plaintext credential field {location}.{key} is forbidden"
                    )
                ControlPlaneService._reject_nested_secrets(
                    item, f"{location}.{key}"
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                ControlPlaneService._reject_nested_secrets(
                    item, f"{location}[{index}]"
                )

    def _decorate_account(self, account: dict[str, Any]) -> dict[str, Any]:
        account = dict(account)
        account["credential_available"] = self.vault.is_available(
            account.get("credential_env_ref")
        )
        account["active_runs"] = self.scheduler.active_jobs_for_account(account["id"])
        account["max_parallel"] = self.scheduler.max_jobs_per_account
        # Legacy local-estimate columns remain in SQLite only for forward
        # compatibility. API consumers receive Kaggle's official quota alone.
        for field in (
            "weekly_quota_hours", "used_hours_estimate", "quota_period_started_at",
            "quota_exhausted", "quota_remaining_hours", "gpu_quota_used_hours",
            "gpu_quota_remaining_hours", "gpu_quota_total_hours",
            "tpu_quota_used_hours", "tpu_quota_remaining_hours",
            "tpu_quota_total_hours", "official_quota_refresh_at",
            "official_quota_synced_at", "official_quota_sync_error",
        ):
            account.pop(field, None)
        return account

    def inspect_credential(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a credential reference and discover its Kaggle username."""
        self._reject_secrets(payload)
        credential_ref = validate_env_ref(
            _required_text(payload, "credential_env_ref", 200)
        )
        username_hint, credentials = self.vault.credential_identity_hint(credential_ref)
        username = username_hint
        if not username:
            kaggle = shutil.which("kaggle")
            if not kaggle:
                raise ValidationError("Kaggle CLI is not installed")
            config_dir = (self.data_dir / "credential-inspection" / credential_ref).resolve()
            config_dir.mkdir(parents=True, exist_ok=True)
            env = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in {
                    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
                    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PYTHONIOENCODING",
                }
            }
            env.update(credentials)
            env["KAGGLE_CONFIG_DIR"] = str(config_dir)
            env["PYTHONIOENCODING"] = "utf-8"
            try:
                completed = subprocess.run(
                    [kaggle, "config", "view"],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                    **hidden_subprocess_kwargs(),
                )
            except subprocess.TimeoutExpired as exc:
                raise ValidationError("Kaggle credential inspection timed out") from exc
            output = f"{completed.stdout}\n{completed.stderr}"
            match = re.search(r"(?im)^-\s*username:\s*([^\s]+)\s*$", output)
            if completed.returncode != 0 or not match:
                raise ValidationError(
                    "Kaggle could not validate this credential; regenerate the API token and try again"
                )
            username = match.group(1)
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValidationError("Kaggle returned an unsupported username")
        return {
            "credential_env_ref": credential_ref,
            "kaggle_username": username,
            "recommended_owner_name": username,
            "recommended_consent_confirmed_by": username,
        }

    def list_credential_refs(self) -> list[dict[str, Any]]:
        raw_refs = os.environ.get("KCP_CREDENTIAL_REFS", "")
        refs = [item.strip() for item in raw_refs.split(",") if item.strip()]
        registered = {
            account.get("credential_env_ref")
            for account in self.database.list_accounts()
            if account.get("state") != "revoked"
        }
        return [
            {
                "credential_env_ref": ref,
                "available": self.vault.is_available(ref),
                "registered": ref in registered,
            }
            for ref in refs
            if re.fullmatch(r"KCP_[A-Za-z0-9_]+", ref)
        ]

    def browse_sources(self, requested_path: str | None = None) -> dict[str, Any]:
        root = self.allowed_source_root
        candidate = root if not requested_path else Path(requested_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            raise ValidationError("source browser does not follow symbolic links")
        current = candidate.resolve()
        if not current.is_relative_to(root):
            raise ValidationError("source path must stay beneath the allowed source root")
        if not current.is_dir():
            raise ValidationError("source path must be a directory")
        directories = []
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ValidationError("source directory cannot be read") from exc
        for child in children:
            try:
                if child.is_dir() and not child.is_symlink():
                    directories.append({
                        "name": child.name,
                        "path": str(child.resolve()),
                        "has_kernel_metadata": (child / "kernel-metadata.json").is_file(),
                    })
            except OSError:
                continue
        return {
            "root": str(root),
            "current": str(current),
            "parent": str(current.parent) if current != root else None,
            "selectable": (current / "kernel-metadata.json").is_file(),
            "directories": directories,
        }

    def create_account(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._reject_secrets(payload)
        username = _required_text(payload, "kaggle_username", 50)
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValidationError("kaggle_username contains unsupported characters")
        credential_ref = validate_env_ref(
            _required_text(payload, "credential_env_ref", 200)
        )
        state = payload.get("state", "enabled")
        if state not in {"enabled", "disabled"}:
            raise ValidationError("new account state must be enabled or disabled")
        values = {
            "owner_name": _required_text(payload, "owner_name", 200),
            "kaggle_username": username,
            "credential_env_ref": credential_ref,
            "state": state,
            "consent_confirmed_by": _required_text(
                payload, "consent_confirmed_by", 200
            ),
            "consent_confirmed_at": payload.get("consent_confirmed_at"),
            "consent_note": payload.get("consent_note"),
            "weekly_quota_hours": None,
            "used_hours_estimate": 0,
        }
        account = self._decorate_account(self.database.create_account(values, actor))
        self.scheduler.request_quota_sync()
        return account

    def list_accounts(self) -> list[dict[str, Any]]:
        return [self._decorate_account(item) for item in self.database.list_accounts()]

    def get_account(self, account_id: str) -> dict[str, Any]:
        return self._decorate_account(self.database.get_account(account_id))

    def update_account(
        self, account_id: str, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        self._reject_secrets(payload)
        allowed = {
            "owner_name",
            "kaggle_username",
            "credential_env_ref",
            "state",
            "consent_confirmed_by",
            "consent_confirmed_at",
            "consent_note",
        }
        unexpected = set(payload) - allowed
        if unexpected:
            raise ValidationError("unsupported fields: " + ", ".join(sorted(unexpected)))
        updates = dict(payload)
        for name in {"owner_name", "kaggle_username", "consent_confirmed_by"} & set(updates):
            updates[name] = _required_text(updates, name, 200)
        if "kaggle_username" in updates and not USERNAME_PATTERN.fullmatch(
            updates["kaggle_username"]
        ):
            raise ValidationError("kaggle_username contains unsupported characters")
        if "credential_env_ref" in updates:
            updates["credential_env_ref"] = validate_env_ref(
                _required_text(updates, "credential_env_ref", 200)
            )
        if "state" in updates and updates["state"] not in {"enabled", "disabled"}:
            raise ValidationError("state must be enabled or disabled")
        account = self._decorate_account(
            self.database.update_account(account_id, updates, actor)
        )
        # Disabled is a dispatch gate only. Already active work keeps running;
        # Re-enabling may release queued work.
        self.scheduler.wake()
        return account

    def revoke_account(self, account_id: str, actor: str) -> dict[str, Any]:
        account = self._decorate_account(self.database.revoke_account(account_id, actor))
        self._stop_active_account_jobs(account_id, actor)
        return account

    def _stop_active_account_jobs(self, account_id: str, actor: str) -> None:
        for job in self.database.list_jobs(account_id=account_id):
            if job["status"] in {"submitting", "submitted", "running", "cancel_requested"}:
                if job["status"] != "cancel_requested":
                    try:
                        self.database.request_cancel(job["id"], actor)
                    except ConflictError:
                        continue
                self.scheduler.request_cancel(job["id"])

    def reconcile_account(
        self, account_id: str, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValidationError(
                "confirmed must be true after manually verifying Kaggle remote state"
            )
        unexpected = set(payload) - {"confirmed", "note"}
        if unexpected:
            raise ValidationError("unsupported fields: " + ", ".join(sorted(unexpected)))
        note = payload.get("note")
        if note is not None:
            if not isinstance(note, str) or len(note.strip()) > 1000:
                raise ValidationError("note must be a string of at most 1000 characters")
            note = note.strip() or None
        account, count = self.database.reconcile_account(account_id, actor, note)
        self.scheduler.wake()
        return {
            "account": self._decorate_account(account),
            "reconciled_job_count": count,
        }

    def sync_account_quota(self, account_id: str) -> dict[str, Any]:
        result = self.scheduler.sync_account_quota(account_id)
        if result is None:
            account = self.get_account(account_id)
            error = (account.get("official_quota") or {}).get("sync_error")
            raise ConflictError(f"official Kaggle quota sync failed: {error or 'unknown error'}")
        self.scheduler.wake()
        return {"account": self._decorate_account(result)}

    @staticmethod
    def _ensure_account_dispatchable(
        account: dict[str, Any], *, accelerator: str = "cpu", location: str = "account"
    ) -> None:
        if account["state"] != "enabled":
            raise ConflictError(f"{location} is {account['state']}")
        if account.get("remote_reconciliation_required"):
            raise ConflictError(
                f"{location} requires remote reconciliation before new work"
            )
        if accelerator in {"gpu", "tpu"}:
            official = account.get("official_quota") or {}
            resource = official.get(accelerator) or {}
            if official.get("sync_error") or resource.get("remaining_hours") is None:
                raise ConflictError(
                    f"{location} needs a successful official Kaggle quota sync"
                )
            if resource.get("remaining_hours") <= 0:
                raise ConflictError(
                    f"{location} has exhausted official Kaggle {accelerator.upper()} quota"
                )

    def create_batch(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        name = _required_text(payload, "name", 300)
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise ValidationError("jobs must be a non-empty list")
        if len(raw_jobs) > 10:
            raise ValidationError("round-1 MVP batches may contain at most 10 jobs")
        specs: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_jobs):
            if not isinstance(raw, dict):
                raise ValidationError(f"jobs[{index}] must be an object")
            account_id = _required_text(raw, "account_id", 100)
            account = self.database.get_account(account_id)
            metadata = raw.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise ValidationError(f"jobs[{index}].metadata must be an object")
            metadata = dict(metadata)
            self._reject_nested_secrets(metadata, f"jobs[{index}].metadata")
            accelerator = metadata.get("accelerator", "cpu")
            if accelerator == "auto":
                accelerator = "cpu"
                metadata["accelerator"] = "cpu"
            if accelerator not in {"gpu", "tpu", "cpu"}:
                raise ValidationError(
                    f"jobs[{index}].metadata.accelerator must be gpu, tpu, or cpu"
                )
            machine_shape = metadata.get("machine_shape")
            if accelerator == "gpu":
                machine_shape = machine_shape or DEFAULT_GPU_MACHINE_SHAPE
                if machine_shape not in GPU_MACHINE_SHAPES:
                    raise ValidationError(
                        f"jobs[{index}].metadata.machine_shape must be "
                        f"{DEFAULT_GPU_MACHINE_SHAPE} for GPU jobs"
                    )
                metadata["machine_shape"] = machine_shape
            elif accelerator == "tpu":
                machine_shape = machine_shape or DEFAULT_TPU_MACHINE_SHAPE
                if machine_shape not in TPU_MACHINE_SHAPES:
                    raise ValidationError(
                        f"jobs[{index}].metadata.machine_shape must be "
                        f"{DEFAULT_TPU_MACHINE_SHAPE} for TPU jobs"
                    )
                metadata["machine_shape"] = machine_shape
            elif machine_shape not in {None, ""}:
                raise ValidationError(
                    f"jobs[{index}].metadata.machine_shape is not valid for CPU jobs"
                )
            else:
                metadata.pop("machine_shape", None)
            self._ensure_account_dispatchable(
                account, accelerator=accelerator, location=f"jobs[{index}] account"
            )
            try:
                json.dumps(metadata)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"jobs[{index}].metadata must be JSON serializable"
                ) from exc
            source_input = Path(
                _required_text(raw, "source_dir", 2000)
            ).expanduser()
            if source_input.is_symlink():
                raise ValidationError(f"jobs[{index}].source_dir must not be a symlink")
            source_path = source_input.resolve()
            if not source_path.is_relative_to(self.allowed_source_root):
                raise ValidationError(
                    f"jobs[{index}].source_dir must be beneath allowed source root "
                    f"{self.allowed_source_root}"
                )
            source_dir = str(source_path)
            output_dir = raw.get("output_dir")
            if output_dir is not None:
                raise ValidationError(
                    f"jobs[{index}].output_dir is managed by the control plane; "
                    "custom paths are not allowed"
                )
            specs.append(
                {
                    "account_id": account_id,
                    "experiment_name": _required_text(raw, "experiment_name", 300),
                    "source_dir": source_dir,
                    "kernel_slug": normalize_kernel_slug(
                        account["kaggle_username"],
                        _required_text(raw, "kernel_slug", 200),
                    ),
                    "output_dir": output_dir,
                    "metadata": metadata,
                }
            )
        batch = self.database.create_batch(name, specs, actor, self.artifact_root)
        self.scheduler.wake()
        return self._decorate_batch(batch)

    def list_batches(self) -> list[dict[str, Any]]:
        return [self._decorate_batch(batch) for batch in self.database.list_batches()]

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self._decorate_batch(
            self.database.get_batch(batch_id, include_jobs=True)
        )

    def list_jobs(
        self,
        *,
        batch_id: str | None = None,
        account_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self._decorate_job(job)
            for job in self.database.list_jobs(
                batch_id=batch_id, account_id=account_id, status=status
            )
        ]

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._decorate_job(self.database.get_job(job_id, include_remote_logs=True))

    def job_remote_logs_page(
        self, job_id: str, *, before_id: int | None = None, limit: int = 200
    ) -> dict[str, Any]:
        self.database.get_job(job_id)
        bounded_limit = max(1, min(limit, 500))
        lines = self.database.list_remote_log_lines(
            job_id, limit=bounded_limit + 1, before_id=before_id
        )
        has_more = len(lines) > bounded_limit
        if has_more:
            lines = lines[1:]
        return {
            "logs": lines,
            "before_id": lines[0]["sequence_id"] if lines else None,
            "has_more": has_more,
        }

    def remote_job_status(self, job_id: str) -> dict[str, Any]:
        """Read the remote Kaggle state for an interrupted local monitor.

        Authentication is resolved only by the in-process credential vault and
        is passed directly to the adapter's short-lived child process.  The
        response is redacted before it crosses the local HTTP boundary.
        """
        job = self.database.get_job(job_id)
        account = self.database.get_account(job["account_id"])
        credential_ref = account.get("credential_env_ref")
        if not credential_ref:
            raise ValidationError("assigned account has no credential reference")
        config_dir = (self.scheduler.config_root / f"status-{job_id}").resolve()
        config_dir.mkdir(parents=True, exist_ok=True)
        env = self.vault.build_subprocess_env(
            credential_ref, account["kaggle_username"], config_dir
        )
        remote = self.adapter.status(job, env, threading.Event())
        result = {
            "job_id": job_id,
            "kernel_slug": job["kernel_slug"],
            "remote_state": remote.state,
            "detail": self.scheduler._redact(remote.detail, env),
        }
        if remote.state == "failed":
            diagnostic = self.adapter.diagnostics(job, env, threading.Event())
            files = diagnostic.get("files", [])
            log_tail = ""
            if isinstance(files, list) and files:
                log_path = Path(job["output_dir"]).resolve() / str(files[0])
                if log_path.is_file():
                    log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
            result["diagnostic"] = {
                "files": files,
                "log_tail": self.scheduler._redact(log_tail, env),
            }
            self.database.transition_job(
                job_id,
                {"submitted", "running", "cancel_requested"},
                "failed",
                fields={
                    "error": result["detail"] or "Kaggle reported failure",
                    "remote_may_be_running": 0,
                },
            )
        elif remote.state == "complete":
            output = self.adapter.output(job, env, threading.Event())
            safe_output = self.scheduler._redact(output, env)
            result["output"] = safe_output
            # An on-demand status check can be the first successful query after
            # a transient scheduler timeout.  It has already downloaded the
            # output above, so leave the persisted job in the same terminal
            # state as the normal scheduler path.  Without this transition the
            # UI says "submitted" even though artifacts exist, and a durable
            # chained benchmark can never submit its successor.
            stored = self.database.get_job(job_id)
            if stored["status"] in {"submitted", "running"}:
                prior_result = stored.get("result") or {}
                completed_result = {
                    "submit": prior_result.get("submit"),
                    "remote_status": result["detail"],
                    "output": safe_output,
                }
                if completed_result["submit"] is None:
                    completed_result.pop("submit")
                changed = self.database.transition_job(
                    job_id,
                    {"submitted", "running"},
                    "succeeded",
                    fields={
                        "result": completed_result,
                        "error": None,
                        "remote_may_be_running": 0,
                    },
                )
                if changed:
                    self.database.append_job_event(
                        job_id,
                        "Persisted completed Kaggle status after on-demand check",
                        details={
                            "output_dir": safe_output.get("output_dir"),
                            "file_count": len(safe_output.get("files", [])),
                        },
                    )
            else:
                # The remote result is terminal even if the local job had
                # already been marked uncertain by a transient CLI outage.
                self.database.transition_job(
                    job_id,
                    {"cancel_requested"},
                    "succeeded",
                    fields={
                        "result": {
                            "remote_status": result["detail"],
                            "output": safe_output,
                        },
                        "error": None,
                        "remote_may_be_running": 0,
                    },
                )
        return result

    def cancel_job(self, job_id: str, actor: str) -> dict[str, Any]:
        job, semantics = self.database.request_cancel(job_id, actor)
        self.scheduler.request_cancel(job_id)
        if semantics == "local_monitor_stop_requested":
            # Kaggle's public CLI cannot stop a submitted kernel by slug.  Do
            # not make the operator clear a guard by hand: keep checking the
            # remote job until Kaggle itself reports a terminal state.
            self._schedule_recovered_job_recheck(job_id)
        return {"job": self._decorate_job(job), "cancel_semantics": semantics}

    def retry_job(self, job_id: str, actor: str) -> dict[str, Any]:
        original = self.database.get_job(job_id)
        account = self.database.get_account(original["account_id"])
        self._ensure_account_dispatchable(
            account,
            accelerator=str(original.get("metadata", {}).get("accelerator", "cpu")),
            location="assigned account",
        )
        retry = self.database.retry_job(job_id, actor, self.artifact_root)
        self.scheduler.wake()
        return self._decorate_job(retry)

    def job_result(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        decorated = self._decorate_job(job)
        return {
            "job_id": job_id,
            "status": job["status"],
            "ready": job["status"] in {"succeeded", "failed", "cancelled"},
            "result": job["result"],
            "error": job["error"],
            "output_dir": job["output_dir"],
            "remote_may_be_running": job["remote_may_be_running"],
            "events": job["events"],
            "accelerator": decorated["accelerator"],
            "machine_shape": decorated["machine_shape"],
            "elapsed_seconds": decorated["elapsed_seconds"],
            "runtime": decorated["runtime"],
        }

    def job_logs_download(self, job_id: str) -> tuple[str, Path]:
        job = self.database.get_job(job_id)
        remote_logs = self._failed_remote_logs(job)
        events = self.database.list_job_events(job_id, limit=10000)
        downloads = (self.data_dir / "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        log_path = (downloads / f"{job_id}-logs.log").resolve()
        if log_path.parent != downloads:
            raise ValidationError("invalid job id for log download")
        temporary_path = log_path.with_suffix(".log.tmp")
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write(f"Job: {job['id']}\n")
            output.write(f"Experiment: {job['experiment_name']}\n")
            output.write(f"Kernel: {job['kernel_slug']}\n")
            output.write(f"Status: {job['status']}\n\n")
            for event in events:
                detail = event.get("details") or {}
                detail_text = ""
                if detail:
                    detail_text = " " + json.dumps(
                        detail, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                    )
                output.write(
                    f"{event.get('created_at', '')} "
                    f"[{str(event.get('level', 'info')).upper()}] "
                    f"{event.get('message', '')}{detail_text}\n"
                )
            live_log_bytes = 20 * 1024 * 1024
            live_logs = self.database.list_remote_log_lines(job["id"], limit=50000)
            if live_logs:
                output.write("\n=== Kaggle live output (captured verbatim) ===\n")
                for entry in live_logs:
                    line = str(entry.get("line", ""))
                    encoded = (line + "\n").encode("utf-8", errors="replace")
                    if len(encoded) > live_log_bytes:
                        output.write("[live output download truncated at 20 MiB]\n")
                        break
                    output.write(line + "\n")
                    live_log_bytes -= len(encoded)
            remaining_bytes = 5 * 1024 * 1024
            output_dir = Path(job["output_dir"]).resolve()
            for remote_log in remote_logs[:20]:
                if remaining_bytes <= 0:
                    break
                payload = remote_log.read_bytes()[:remaining_bytes]
                remaining_bytes -= len(payload)
                relative = str(remote_log.relative_to(output_dir))
                output.write(f"\n=== Kaggle remote log: {relative} ===\n")
                output.write(payload.decode("utf-8", errors="replace"))
                if payload and not payload.endswith(b"\n"):
                    output.write("\n")
        temporary_path.replace(log_path)
        return log_path.name, log_path

    def _failed_remote_logs(self, job: dict[str, Any]) -> list[Path]:
        if job["status"] != "failed":
            return []
        output_dir = Path(job["output_dir"]).resolve()
        artifact_root = self.artifact_root.resolve()
        if not output_dir.is_relative_to(artifact_root):
            raise ValidationError("job output directory is outside the managed artifact root")
        existing = (
            sorted(path.resolve() for path in output_dir.rglob("*.log"))
            if output_dir.is_dir()
            else []
        )
        existing = [
            path
            for path in existing
            if (
                path.is_file()
                and path.stat().st_size > 0
                and path.is_relative_to(output_dir)
            )
        ]
        if existing:
            return existing

        account = self.database.get_account(job["account_id"])
        credential_ref = account.get("credential_env_ref")
        if not credential_ref:
            return []
        config_dir = (self.data_dir / "kaggle-config" / job["id"]).resolve()
        config_dir.mkdir(parents=True, exist_ok=True)
        env = self.vault.build_subprocess_env(
            credential_ref, account["kaggle_username"], config_dir
        )
        try:
            raw_output = self.adapter.diagnostics(job, env, threading.Event())
            JobScheduler._redact_downloaded_text_files(raw_output, env)
            safe_output = JobScheduler._redact(raw_output, env)
            self.database.append_job_event(
                job["id"],
                "Downloaded failed Kaggle kernel diagnostics on demand",
                details={"file_count": len(safe_output.get("files", []))},
            )
        except Exception as exc:
            self.database.append_job_event(
                job["id"],
                "Could not download failed Kaggle kernel diagnostics on demand",
                details={"error": JobScheduler._redact(str(exc), env)},
                level="warning",
            )
            return []
        return sorted(
            path.resolve()
            for path in output_dir.rglob("*.log")
            if path.is_file() and path.resolve().is_relative_to(output_dir)
        )

    def job_events_page(
        self, job_id: str, *, before_id: int | None = None, limit: int = 200
    ) -> dict[str, Any]:
        self.database.get_job(job_id)
        bounded_limit = max(1, min(limit, 500))
        events = self.database.list_job_events(
            job_id, limit=bounded_limit + 1, before_id=before_id
        )
        has_more = len(events) > bounded_limit
        if has_more:
            events = events[1:]
        return {
            "events": events,
            "before_id": events[0]["sequence_id"] if events else None,
            "has_more": has_more,
        }

    def job_result_download(self, job_id: str) -> tuple[str, Path]:
        job = self.database.get_job(job_id)
        if job["status"] != "succeeded":
            raise ConflictError("result download is available only after the job succeeds")
        output_dir = Path(job["output_dir"]).resolve()
        artifact_root = self.artifact_root.resolve()
        if not output_dir.is_relative_to(artifact_root):
            raise ValidationError("job output directory is outside the managed artifact root")
        if not output_dir.is_dir():
            raise ConflictError("result artifacts are not available on this machine")

        downloads = (self.data_dir / "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        archive_path = (downloads / f"{job_id}-results.zip").resolve()
        if archive_path.parent != downloads:
            raise ValidationError("invalid job id for result download")
        temporary_path = archive_path.with_suffix(".zip.tmp")
        manifest = {
            "job_id": job["id"],
            "experiment_name": job["experiment_name"],
            "kernel_slug": job["kernel_slug"],
            "status": job["status"],
            "finished_at": job.get("finished_at"),
            "result": job.get("result"),
        }
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.writestr(
                "job-result.json",
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            )
            for path in sorted(output_dir.rglob("*")):
                if path.is_symlink():
                    raise ValidationError("result artifacts must not contain symbolic links")
                if path.is_file():
                    resolved = path.resolve()
                    if not resolved.is_relative_to(output_dir):
                        raise ValidationError("result artifact escapes the managed output directory")
                    archive.write(resolved, arcname=str(resolved.relative_to(output_dir)))
        temporary_path.replace(archive_path)
        return archive_path.name, archive_path

    def list_audit(
        self,
        *,
        limit: int = 100,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.database.list_audit(
            limit=limit, entity_type=entity_type, entity_id=entity_id
        )

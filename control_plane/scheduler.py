"""Concurrent, explicit-account experiment scheduler."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .adapters import (
    AdapterError,
    DEFAULT_MAX_SOURCE_BYTES,
    KaggleAdapter,
    LocalCommandCancelled,
    stage_job_source,
)
from .credentials import EnvCredentialVault
from .database import ACTIVE_JOB_STATES, Database


class JobScheduler:
    def __init__(
        self,
        database: Database,
        adapter: KaggleAdapter,
        vault: EnvCredentialVault,
        data_dir: str | Path,
        *,
        max_workers: int = 10,
        max_jobs_per_account: int = 2,
        remote_poll_seconds: float = 5.0,
        dispatch_poll_seconds: float = 0.1,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
        quota_sync_seconds: float = 300.0,
        quota_start_delay_seconds: float = 0.0,
    ):
        self.database = database
        self.adapter = adapter
        self.vault = vault
        self.data_dir = Path(data_dir).resolve()
        self.staging_root = self.data_dir / "staging"
        self.config_root = self.data_dir / "kaggle-config"
        self.max_workers = max(1, max_workers)
        self.max_jobs_per_account = max(1, max_jobs_per_account)
        self.remote_poll_seconds = max(0.001, remote_poll_seconds)
        self.dispatch_poll_seconds = max(0.001, dispatch_poll_seconds)
        self.max_source_bytes = max(1, max_source_bytes)
        self.quota_sync_seconds = max(30.0, quota_sync_seconds)
        self.quota_start_delay_seconds = max(0.0, quota_start_delay_seconds)
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="kcp-job"
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._quota_wake = threading.Event()
        self._lock = threading.Lock()
        self._active_account_jobs: dict[str, int] = {}
        self._futures: dict[str, Future[None]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="kcp-dispatcher", daemon=True
        )
        self._quota_thread = threading.Thread(
            target=self._quota_loop, name="kcp-quota-sync", daemon=True
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        self._quota_thread.start()

    def wake(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running_local_jobs": len(self._futures),
                "active_accounts": len(self._active_account_jobs),
                "max_workers": self.max_workers,
                "max_jobs_per_account": self.max_jobs_per_account,
            }

    def active_jobs_for_account(self, account_id: str) -> int:
        with self._lock:
            return self._active_account_jobs.get(account_id, 0)

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            cancel_event = self._cancel_events.get(job_id)
        if cancel_event:
            cancel_event.set()
        self.wake()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._quota_wake.set()
        with self._lock:
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
        if self._started:
            self._thread.join(timeout=5)
            self._quota_thread.join(timeout=5)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch_available()
            except Exception as exc:  # keep the dispatcher alive; record diagnostic
                self.database.append_audit(
                    "scheduler",
                    "scheduler.dispatch_error",
                    "scheduler",
                    "local",
                    {"error": str(exc)[:2000]},
                )
            self._wake.wait(self.dispatch_poll_seconds)
            self._wake.clear()

    def _quota_loop(self) -> None:
        # Cached official values are available immediately. Delay the network
        # refresh briefly on desktop startup so Kaggle CLI processes do not
        # contend with the dashboard's first render. Explicit sync requests
        # still wake this thread immediately.
        if self.quota_start_delay_seconds:
            self._quota_wake.wait(self.quota_start_delay_seconds)
            self._quota_wake.clear()
        while not self._stop.is_set():
            for account in self.database.list_accounts():
                if self._stop.is_set():
                    return
                if account.get("state") == "revoked" or not account.get("credential_env_ref"):
                    continue
                self.sync_account_quota(account["id"])
            self.wake()
            self._quota_wake.wait(self.quota_sync_seconds)
            self._quota_wake.clear()

    def request_quota_sync(self) -> None:
        self._quota_wake.set()

    def sync_account_quota(self, account_id: str) -> dict[str, Any] | None:
        account = self.database.get_account(account_id)
        credential_ref = account.get("credential_env_ref")
        if not credential_ref:
            return None
        try:
            config_dir = (self.config_root / f"quota-{account_id}").resolve()
            config_dir.mkdir(parents=True, exist_ok=True)
            env = self.vault.build_subprocess_env(
                credential_ref, account["kaggle_username"], config_dir
            )
            quota = self.adapter.quota(env, self._stop)
            return self.database.update_official_quota(account_id, quota)
        except Exception as exc:
            if not self._stop.is_set():
                self.database.mark_official_quota_error(account_id, str(exc))
            return None

    @staticmethod
    def _quota_blocks(account: dict[str, Any], accelerator: str) -> bool:
        if accelerator not in {"gpu", "tpu"}:
            return False
        official = account.get("official_quota") or {}
        resource = official.get(accelerator) or {}
        remaining = resource.get("remaining_hours")
        # Accelerator jobs require a successful official Kaggle sync. Stale
        # real values remain visible, but a current sync error blocks dispatch.
        return official.get("sync_error") is not None or remaining is None or remaining <= 0

    def _dispatch_available(self) -> None:
        with self._lock:
            capacity = self.max_workers - len(self._futures)
            active_account_jobs = dict(self._active_account_jobs)
        if capacity <= 0:
            return
        for job in self.database.queued_jobs(limit=max(100, capacity * 4)):
            if capacity <= 0:
                break
            account_id = job["account_id"]
            if active_account_jobs.get(account_id, 0) >= self.max_jobs_per_account:
                continue
            account = self.database.get_account(account_id)
            if account["state"] == "revoked":
                self.database.transition_job(
                    job["id"],
                    {"queued"},
                    "failed",
                    fields={"error": "assigned account is revoked"},
                )
                continue
            if (
                account["state"] == "disabled"
                or account.get("remote_reconciliation_required")
                or self._quota_blocks(
                    account, str(job.get("metadata", {}).get("accelerator", "cpu"))
                )
            ):
                # These are reversible gates. Preserve queued work until the
                # account is enabled, reconciled, or its quota period resets.
                continue
            claimed = self.database.transition_job(
                job["id"], {"queued"}, "submitting"
            )
            if not claimed:
                continue
            cancel_event = threading.Event()
            with self._lock:
                # There is only one dispatcher, and the account is reserved
                # before another queued job can be considered.
                self._active_account_jobs[account_id] = (
                    self._active_account_jobs.get(account_id, 0) + 1
                )
                self._cancel_events[job["id"]] = cancel_event
                future = self._executor.submit(
                    self._run_job, job["id"], account, cancel_event
                )
                self._futures[job["id"]] = future
            future.add_done_callback(
                lambda completed, jid=job["id"], aid=account_id: self._job_done(
                    jid, aid, completed
                )
            )
            active_account_jobs[account_id] = active_account_jobs.get(account_id, 0) + 1
            capacity -= 1

    def _job_done(
        self, job_id: str, account_id: str, future: Future[None]
    ) -> None:
        if future.cancelled():
            self.database.transition_job(
                job_id,
                ACTIVE_JOB_STATES,
                "cancelled",
                fields={"error": "local scheduler stopped before worker execution"},
            )
            exception = None
        else:
            exception = future.exception()
        if exception:
            # _run_job catches operational failures; this branch is a final
            # guard for programmer/runtime failures.
            self.database.transition_job(
                job_id,
                ACTIVE_JOB_STATES,
                "failed",
                fields={"error": f"scheduler worker crashed: {exception}"[:2000]},
            )
        with self._lock:
            self._futures.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            remaining = self._active_account_jobs.get(account_id, 1) - 1
            if remaining > 0:
                self._active_account_jobs[account_id] = remaining
            else:
                self._active_account_jobs.pop(account_id, None)
        self._quota_wake.set()
        self.wake()

    def _run_job(
        self,
        job_id: str,
        account: dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        submitted = False
        submit_started = False
        submit_result: dict[str, Any] | None = None
        env: dict[str, str] | None = None
        try:
            job = self.database.get_job(job_id)
            # Re-fetch after claim. Disable is intentionally a new-dispatch
            # gate only, so an already claimed job continues; revoke remains
            # a strong stop. Reconciliation/quota races put the job back.
            account = self.database.get_account(job["account_id"])
            if account["state"] == "revoked":
                raise AdapterError("assigned account is revoked")
            if account.get("remote_reconciliation_required") or self._quota_blocks(
                account, str(job.get("metadata", {}).get("accelerator", "cpu"))
            ):
                self.database.transition_job(job_id, {"submitting"}, "queued")
                return
            staged_job = stage_job_source(
                job,
                account,
                self.staging_root,
                max_source_bytes=self.max_source_bytes,
            )
            self.database.append_job_event(
                job_id,
                "Prepared an isolated source staging directory",
                details={"kernel_slug": staged_job["kernel_slug"]},
            )
            config_dir = (self.config_root / job_id).resolve()
            config_dir.mkdir(parents=True, exist_ok=True)
            credential_ref = account.get("credential_env_ref")
            if not credential_ref:
                raise AdapterError("assigned account has no credential reference")
            env = self.vault.build_subprocess_env(
                credential_ref, account["kaggle_username"], config_dir
            )
            latest_account = self.database.get_account(job["account_id"])
            if latest_account["state"] == "revoked":
                raise LocalCommandCancelled("assigned account was revoked")
            if latest_account.get("remote_reconciliation_required") or self._quota_blocks(
                latest_account, str(job.get("metadata", {}).get("accelerator", "cpu"))
            ):
                self.database.transition_job(job_id, {"submitting"}, "queued")
                return
            # Close the credential resolution window: revocation and explicit
            # cancellation signal this event before returning. Disable does not.
            if cancel_event.is_set():
                raise LocalCommandCancelled("assigned account was revoked or job cancelled")
            submit_started = True
            raw_submit_result = self.adapter.submit(staged_job, env, cancel_event)
            submit_result = self._redact(raw_submit_result, env)
            self.database.append_job_event(
                job_id,
                "Submitted the staged kernel to Kaggle",
                details={"submit": submit_result},
            )
            submitted = True
            if cancel_event.is_set():
                raise LocalCommandCancelled("cancel requested after submit")
            changed = self.database.transition_job(
                job_id, {"submitting"}, "submitted"
            )
            if not changed:
                raise LocalCommandCancelled("job state changed while submitting")

            seen_running = False
            last_remote_state: str | None = None
            while not cancel_event.is_set():
                remote = self.adapter.status(staged_job, env, cancel_event)
                safe_detail = self._redact(remote.detail, env)
                if remote.state != last_remote_state:
                    self.database.append_job_event(
                        job_id,
                        f"Kaggle remote status: {remote.state}",
                        details={"detail": safe_detail},
                        level="error" if remote.state == "failed" else "info",
                    )
                    last_remote_state = remote.state
                if remote.state == "complete":
                    output_result = self._redact(
                        self.adapter.output(staged_job, env, cancel_event), env
                    )
                    self.database.append_job_event(
                        job_id,
                        "Downloaded job output",
                        details={
                            "output_dir": output_result.get("output_dir"),
                            "file_count": len(output_result.get("files", [])),
                        },
                    )
                    result = {
                        "submit": submit_result,
                        "remote_status": safe_detail,
                        "output": output_result,
                    }
                    changed = self.database.transition_job(
                        job_id,
                        {"submitted", "running"},
                        "succeeded",
                        fields={"result": result, "error": None},
                    )
                    if not changed and self.database.get_job(job_id)["status"] == "cancel_requested":
                        raise LocalCommandCancelled("local monitor stop requested")
                    return
                if remote.state == "failed":
                    changed = self.database.transition_job(
                        job_id,
                        {"submitted", "running"},
                        "failed",
                        fields={
                            "error": safe_detail or "Kaggle reported failure",
                            "result": {"submit": submit_result},
                        },
                    )
                    if not changed and self.database.get_job(job_id)["status"] == "cancel_requested":
                        raise LocalCommandCancelled("local monitor stop requested")
                    return
                if remote.state == "cancelled":
                    changed = self.database.transition_job(
                        job_id,
                        {"submitted", "running"},
                        "cancelled",
                        fields={
                            "error": safe_detail or "Kaggle reported cancellation",
                            "result": {"submit": submit_result},
                        },
                    )
                    if not changed and self.database.get_job(job_id)["status"] == "cancel_requested":
                        raise LocalCommandCancelled("local monitor stop requested")
                    return
                if remote.state == "running" and not seen_running:
                    self.database.transition_job(
                        job_id, {"submitted"}, "running"
                    )
                    seen_running = True
                if cancel_event.wait(self.remote_poll_seconds):
                    break
            raise LocalCommandCancelled("local monitor stop requested")
        except LocalCommandCancelled as exc:
            safe_error = self._redact(str(exc), env)
            self.database.append_job_event(
                job_id, safe_error, level="warning"
            )
            current = self.database.get_job(job_id)
            remote_uncertain = bool(
                current.get("remote_may_be_running") or submit_started
            )
            self.database.transition_job(
                job_id,
                ACTIVE_JOB_STATES,
                "cancelled",
                fields={
                    "error": safe_error,
                    "remote_may_be_running": 1 if remote_uncertain else 0,
                    "cancel_requested": 1,
                    "result": {"submit": submit_result} if submit_result else None,
                },
            )
        except Exception as exc:
            safe_error = self._redact(str(exc), env)[:2000]
            self.database.append_job_event(job_id, safe_error, level="error")
            current = self.database.get_job(job_id)
            if current["status"] == "cancel_requested":
                self.database.transition_job(
                    job_id,
                    {"cancel_requested"},
                    "cancelled",
                    fields={
                        "error": "local monitor stopped; remote execution may continue",
                        "remote_may_be_running": 1,
                        "cancel_requested": 1,
                    },
                )
            else:
                self.database.transition_job(
                    job_id,
                    ACTIVE_JOB_STATES,
                    "failed",
                    fields={
                        "error": safe_error,
                        "remote_may_be_running": 1 if submit_started else 0,
                        "result": {"submit": submit_result} if submit_result else None,
                    },
                )

    @staticmethod
    def _redact(value: Any, env: dict[str, str] | None) -> Any:
        secrets = []
        if env:
            secrets = [
                env[key]
                for key in ("KAGGLE_API_TOKEN", "KAGGLE_KEY")
                if env.get(key)
            ]
        if isinstance(value, str):
            result = value
            for secret in secrets:
                result = result.replace(secret, "[REDACTED]")
            return result[:8000]
        if isinstance(value, dict):
            return {
                str(key)[:200]: JobScheduler._redact(item, env)
                for key, item in list(value.items())[:1000]
            }
        if isinstance(value, list):
            return [JobScheduler._redact(item, env) for item in value[:1000]]
        if isinstance(value, tuple):
            return [JobScheduler._redact(item, env) for item in value[:1000]]
        return value

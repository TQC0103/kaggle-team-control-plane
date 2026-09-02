"""SQLite persistence for accounts, experiment batches, jobs and audit events."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import ConflictError, NotFoundError


TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}
ACTIVE_JOB_STATES = {"submitting", "submitted", "running", "cancel_requested"}
LEGACY_RESTART_FAILURE = (
    "control plane restarted while this job was active; verify Kaggle remotely"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock, self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    owner_name TEXT NOT NULL,
                    kaggle_username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    credential_env_ref TEXT,
                    state TEXT NOT NULL CHECK(state IN ('enabled','disabled','revoked')),
                    consent_confirmed_by TEXT NOT NULL,
                    consent_confirmed_at TEXT NOT NULL,
                    consent_note TEXT,
                    weekly_quota_hours REAL,
                    used_hours_estimate REAL NOT NULL DEFAULT 0,
                    quota_period_started_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id),
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    experiment_name TEXT NOT NULL,
                    source_dir TEXT NOT NULL,
                    kernel_slug TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','submitting','submitted','running',
                        'cancel_requested','succeeded','failed','cancelled'
                    )),
                    attempt INTEGER NOT NULL DEFAULT 1,
                    retry_of_job_id TEXT REFERENCES jobs(id),
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    remote_may_be_running INTEGER NOT NULL DEFAULT 0,
                    quota_accounted INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    remote_started_at TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS jobs_status_created_idx
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS jobs_account_status_idx
                    ON jobs(account_id, status);
                CREATE INDEX IF NOT EXISTS jobs_batch_idx ON jobs(batch_id);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS job_events_job_idx
                    ON job_events(job_id, id DESC);

                -- Live Kaggle output is deliberately stored outside
                -- ``job_events``.  A single CLI snapshot can contain hundreds
                -- of lines, which used to overflow the small JSON details
                -- field and silently turn the whole event into
                -- {"truncated": true}.  These are already redacted by the
                -- scheduler before they reach the database.
                CREATE TABLE IF NOT EXISTS job_remote_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    line TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS job_remote_logs_job_idx
                    ON job_remote_logs(job_id, id DESC);

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS audit_entity_idx
                    ON audit_logs(entity_type, entity_id, id DESC);
                """
            )
            # Lightweight forward migration for databases created by an early
            # MVP build before quota_accounted was introduced.
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "quota_accounted" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN quota_accounted INTEGER NOT NULL DEFAULT 0"
                )
            if "remote_started_at" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN remote_started_at TEXT")
            account_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
            }
            if "quota_period_started_at" not in account_columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN quota_period_started_at TEXT"
                )
                connection.execute(
                    "UPDATE accounts SET quota_period_started_at=created_at "
                    "WHERE quota_period_started_at IS NULL"
                )
            official_quota_columns = {
                "gpu_quota_used_hours": "REAL",
                "gpu_quota_remaining_hours": "REAL",
                "gpu_quota_total_hours": "REAL",
                "tpu_quota_used_hours": "REAL",
                "tpu_quota_remaining_hours": "REAL",
                "tpu_quota_total_hours": "REAL",
                "official_quota_refresh_at": "TEXT",
                "official_quota_synced_at": "TEXT",
                "official_quota_sync_error": "TEXT",
            }
            for name, column_type in official_quota_columns.items():
                if name not in account_columns:
                    connection.execute(
                        f"ALTER TABLE accounts ADD COLUMN {name} {column_type}"
                    )

    def append_job_event(
        self,
        job_id: str,
        message: str,
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        event_id = new_id("log")
        now = utc_now()
        safe_message = str(message).replace("\x00", "")[:2000]
        details_json = json.dumps(details or {}, separators=(",", ":"), sort_keys=True)
        if len(details_json) > 8000:
            details_json = '{"truncated":true}'

        def insert(target: sqlite3.Connection) -> None:
            target.execute(
                "INSERT INTO job_events "
                "(event_id,job_id,level,message,details_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (event_id, job_id, level[:20], safe_message, details_json, now),
            )
            # Keep a generous but bounded trace. The dashboard pages through
            # it, while downloads can include the full retained history.
            target.execute(
                "DELETE FROM job_events WHERE job_id=? AND id NOT IN "
                "(SELECT id FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 10000)",
                (job_id, job_id),
            )

        if connection is not None:
            insert(connection)
        else:
            with self.connection() as owned:
                insert(owned)
        return {
            "event_id": event_id,
            "job_id": job_id,
            "level": level[:20],
            "message": safe_message,
            "details": details or {},
            "created_at": now,
        }

    def list_job_events(
        self, job_id: str, limit: int = 200, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 10000))
        with self.connection() as connection:
            if before_id is None:
                rows = connection.execute(
                    "SELECT id,event_id,job_id,level,message,details_json,created_at "
                    "FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                    (job_id, bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,event_id,job_id,level,message,details_json,created_at "
                    "FROM job_events WHERE job_id=? AND id<? ORDER BY id DESC LIMIT ?",
                    (job_id, before_id, bounded_limit),
                ).fetchall()
        rows = list(reversed(rows))
        return [
            {
                "sequence_id": row["id"],
                "event_id": row["event_id"],
                "job_id": row["job_id"],
                "level": row["level"],
                "message": row["message"],
                "details": json.loads(row["details_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def append_remote_log_lines(self, job_id: str, lines: list[str]) -> int:
        """Persist bounded, already-redacted Kaggle output line by line.

        Keeping the original line boundaries lets the dashboard render the
        actual Kaggle stream instead of a lossy scheduler-event summary.
        """
        now = utc_now()
        prepared = [
            (job_id, str(line).replace("\x00", "")[:32768], now)
            for line in lines
        ]
        if not prepared:
            return 0
        with self.connection() as connection:
            connection.executemany(
                "INSERT INTO job_remote_logs (job_id,line,created_at) VALUES (?,?,?)",
                prepared,
            )
            # Enough headroom for long scientific runs, but a noisy notebook
            # must not grow the desktop database forever.
            connection.execute(
                "DELETE FROM job_remote_logs WHERE job_id=? AND id NOT IN "
                "(SELECT id FROM job_remote_logs WHERE job_id=? "
                "ORDER BY id DESC LIMIT 50000)",
                (job_id, job_id),
            )
        return len(prepared)

    def replace_remote_log_lines(self, job_id: str, lines: list[str]) -> int:
        """Atomically replace the live tail with Kaggle's terminal log.

        A bounded ``--follow`` snapshot can start mid-stream or end in the
        middle of a progress line.  Once Kaggle publishes its immutable final
        log, it is the authoritative version rendered by the web UI.
        """
        now = utc_now()
        prepared = [
            (job_id, str(line).replace("\x00", "")[:32768], now)
            for line in lines
        ][-50000:]
        with self.connection() as connection:
            connection.execute("DELETE FROM job_remote_logs WHERE job_id=?", (job_id,))
            if prepared:
                connection.executemany(
                    "INSERT INTO job_remote_logs (job_id,line,created_at) VALUES (?,?,?)",
                    prepared,
                )
        return len(prepared)

    def list_remote_log_lines(
        self, job_id: str, limit: int = 200, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 1000))
        with self.connection() as connection:
            if before_id is None:
                rows = connection.execute(
                    "SELECT id,line,created_at FROM job_remote_logs "
                    "WHERE job_id=? ORDER BY id DESC LIMIT ?",
                    (job_id, bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,line,created_at FROM job_remote_logs "
                    "WHERE job_id=? AND id<? ORDER BY id DESC LIMIT ?",
                    (job_id, before_id, bounded_limit),
                ).fetchall()
        return [
            {
                "sequence_id": row["id"],
                "line": row["line"],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    @staticmethod
    def _decode(row: sqlite3.Row | None, kind: str) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        if kind == "job":
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            result_raw = item.pop("result_json")
            item["result"] = json.loads(result_raw) if result_raw else None
            item["cancel_requested"] = bool(item["cancel_requested"])
            item["remote_may_be_running"] = bool(item["remote_may_be_running"])
            item["quota_accounted"] = bool(item["quota_accounted"])
        elif kind == "audit":
            item["details"] = json.loads(item.pop("details_json") or "{}")
        return item

    def append_audit(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        details: dict[str, Any] | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        event_id = new_id("evt")
        now = utc_now()
        values = (
            event_id,
            actor,
            action,
            entity_type,
            entity_id,
            json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
            now,
        )
        if connection is not None:
            connection.execute(
                "INSERT INTO audit_logs "
                "(event_id,actor,action,entity_type,entity_id,details_json,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                values,
            )
        else:
            with self.connection() as owned:
                owned.execute(
                    "INSERT INTO audit_logs "
                    "(event_id,actor,action,entity_type,entity_id,details_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    values,
                )
        return {
            "event_id": event_id,
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": details or {},
            "created_at": now,
        }

    def create_account(self, values: dict[str, Any], actor: str) -> dict[str, Any]:
        account_id = new_id("acct")
        now = utc_now()
        try:
            with self.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO accounts (
                        id, owner_name, kaggle_username, credential_env_ref, state,
                        consent_confirmed_by, consent_confirmed_at, consent_note,
                        weekly_quota_hours, used_hours_estimate,
                        quota_period_started_at, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        account_id,
                        values["owner_name"],
                        values["kaggle_username"],
                        values["credential_env_ref"],
                        values.get("state", "enabled"),
                        values["consent_confirmed_by"],
                        values.get("consent_confirmed_at") or now,
                        values.get("consent_note"),
                        values.get("weekly_quota_hours"),
                        values.get("used_hours_estimate", 0),
                        now,
                        now,
                        now,
                    ),
                )
                self.append_audit(
                    actor,
                    "account.created",
                    "account",
                    account_id,
                    {"kaggle_username": values["kaggle_username"]},
                    connection=connection,
                )
        except sqlite3.IntegrityError as exc:
            if "kaggle_username" in str(exc):
                raise ConflictError("kaggle_username already exists") from exc
            raise
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        account = self._decode(row, "account")
        if not account:
            raise NotFoundError(f"account {account_id!r} was not found")
        return self._decorate_account_runtime(account)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM accounts ORDER BY created_at, id"
            ).fetchall()
        return [
            self._decorate_account_runtime(self._decode(row, "account"))  # type: ignore[arg-type]
            for row in rows
        ]

    def _decorate_account_runtime(self, account: dict[str, Any]) -> dict[str, Any]:
        account = dict(account)
        account_id = account["id"]
        with self.connection() as connection:
            # After a Control Plane restart, an active job can still be running
            # on Kaggle even though there is no in-process scheduler worker for
            # it yet. Treat that as unresolved as well: otherwise a restart
            # could allow two more GPU submissions and exceed the account's
            # actual remote concurrency.
            unresolved_rows = connection.execute(
                "SELECT status FROM jobs WHERE account_id=? "
                "AND remote_may_be_running=1",
                (account_id,),
            ).fetchall()
        # Keep the dispatch guard, but expose *why* an account is guarded.
        # A confirmed remote run is not something the operator can or should
        # manually "unlock".  Only a terminal local record whose remote state
        # is still unknown is a genuine uncertainty.
        remote_active_runs = sum(
            row["status"] in ACTIVE_JOB_STATES for row in unresolved_rows
        )
        account["remote_active_runs"] = remote_active_runs
        account["remote_terminal_uncertainties"] = len(unresolved_rows) - remote_active_runs
        account["remote_reconciliation_required"] = bool(unresolved_rows)
        account["official_quota"] = {
            "source": "kaggle",
            "synced_at": account.get("official_quota_synced_at"),
            "refresh_at": account.get("official_quota_refresh_at"),
            "sync_error": account.get("official_quota_sync_error"),
            "gpu": {
                "used_hours": account.get("gpu_quota_used_hours"),
                "remaining_hours": account.get("gpu_quota_remaining_hours"),
                "total_hours": account.get("gpu_quota_total_hours"),
            },
            "tpu": {
                "used_hours": account.get("tpu_quota_used_hours"),
                "remaining_hours": account.get("tpu_quota_remaining_hours"),
                "total_hours": account.get("tpu_quota_total_hours"),
            },
        }
        return account

    def update_official_quota(
        self, account_id: str, quota: dict[str, Any], actor: str = "quota-sync"
    ) -> dict[str, Any]:
        now = utc_now()
        resources = quota.get("resources", {})
        gpu = resources.get("gpu", {})
        tpu = resources.get("tpu", {})
        with self.connection() as connection:
            connection.execute(
                "UPDATE accounts SET gpu_quota_used_hours=?,gpu_quota_remaining_hours=?,"
                "gpu_quota_total_hours=?,tpu_quota_used_hours=?,"
                "tpu_quota_remaining_hours=?,tpu_quota_total_hours=?,"
                "official_quota_refresh_at=?,official_quota_synced_at=?,"
                "official_quota_sync_error=NULL,updated_at=? WHERE id=?",
                (
                    gpu.get("used_hours"), gpu.get("remaining_hours"), gpu.get("total_hours"),
                    tpu.get("used_hours"), tpu.get("remaining_hours"), tpu.get("total_hours"),
                    quota.get("refresh_at"), now, now, account_id,
                ),
            )
            self.append_audit(
                actor, "account.quota_synced", "account", account_id,
                {"source": "kaggle", "refresh_at": quota.get("refresh_at")},
                connection=connection,
            )
        return self.get_account(account_id)

    def mark_official_quota_error(self, account_id: str, error: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE accounts SET official_quota_sync_error=?,updated_at=? WHERE id=?",
                (str(error)[:1000], utc_now(), account_id),
            )

    def update_account(
        self, account_id: str, updates: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        current = self.get_account(account_id)
        if current["state"] == "revoked":
            raise ConflictError("a revoked account cannot be modified")
        if not updates:
            return current
        updates = dict(updates)
        updates["updated_at"] = utc_now()
        columns = ", ".join(f"{key} = ?" for key in updates)
        try:
            with self.connection() as connection:
                connection.execute(
                    f"UPDATE accounts SET {columns} WHERE id = ?",  # noqa: S608
                    (*updates.values(), account_id),
                )
                self.append_audit(
                    actor,
                    "account.updated",
                    "account",
                    account_id,
                    {"fields": sorted(key for key in updates if key != "updated_at")},
                    connection=connection,
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("kaggle_username already exists") from exc
        return self.get_account(account_id)

    def revoke_account(self, account_id: str, actor: str) -> dict[str, Any]:
        current = self.get_account(account_id)
        if current["state"] == "revoked":
            return current
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "UPDATE accounts SET state='revoked', credential_env_ref=NULL, "
                "revoked_at=?, updated_at=? WHERE id=?",
                (now, now, account_id),
            )
            self.append_audit(
                actor,
                "account.revoked",
                "account",
                account_id,
                {"credential_reference_removed": True},
                connection=connection,
            )
        return self.get_account(account_id)

    def reconcile_account(
        self, account_id: str, actor: str, note: str | None = None
    ) -> tuple[dict[str, Any], int]:
        self.get_account(account_id)
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM jobs WHERE account_id=? "
                "AND status IN ('succeeded','failed','cancelled') "
                "AND remote_may_be_running=1 ORDER BY rowid",
                (account_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET remote_may_be_running=0,updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self.append_job_event(
                    row["id"],
                    "Remote Kaggle state manually reconciled",
                    details={"note": note} if note else {},
                    connection=connection,
                )
            self.append_audit(
                actor,
                "account.remote_reconciled",
                "account",
                account_id,
                {
                    "reconciled_job_count": len(rows),
                    "note": note,
                },
                connection=connection,
            )
        return self.get_account(account_id), len(rows)

    def create_batch(
        self,
        name: str,
        job_specs: list[dict[str, Any]],
        actor: str,
        default_output_root: Path,
    ) -> dict[str, Any]:
        batch_id = new_id("batch")
        now = utc_now()
        jobs: list[dict[str, Any]] = []
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO batches (id,name,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (batch_id, name, actor, now, now),
            )
            for spec in job_specs:
                job_id = new_id("job")
                output_dir = spec.get("output_dir") or str(
                    (default_output_root / job_id).resolve()
                )
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id,batch_id,account_id,experiment_name,source_dir,kernel_slug,
                        output_dir,status,attempt,retry_of_job_id,metadata_json,
                        created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,'queued',1,NULL,?,?,?)
                    """,
                    (
                        job_id,
                        batch_id,
                        spec["account_id"],
                        spec["experiment_name"],
                        spec["source_dir"],
                        spec["kernel_slug"],
                        output_dir,
                        json.dumps(spec.get("metadata") or {}, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                self.append_audit(
                    actor,
                    "job.queued",
                    "job",
                    job_id,
                    {"batch_id": batch_id, "account_id": spec["account_id"]},
                    connection=connection,
                )
                self.append_job_event(
                    job_id,
                    "Job queued for its explicitly assigned account",
                    details={"account_id": spec["account_id"], "batch_id": batch_id},
                    connection=connection,
                )
            self.append_audit(
                actor,
                "batch.created",
                "batch",
                batch_id,
                {"job_count": len(job_specs)},
                connection=connection,
            )
        return self.get_batch(batch_id, include_jobs=True)

    def get_batch(self, batch_id: str, include_jobs: bool = False) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"batch {batch_id!r} was not found")
        batch = dict(row)
        jobs = self.list_jobs(batch_id=batch_id)
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        batch["status"] = self._batch_status(jobs)
        batch["job_counts"] = counts
        if include_jobs:
            batch["jobs"] = jobs
        return batch

    @staticmethod
    def _batch_status(jobs: list[dict[str, Any]]) -> str:
        if not jobs or all(job["status"] == "queued" for job in jobs):
            return "queued"
        states = {job["status"] for job in jobs}
        if states <= {"succeeded"}:
            return "succeeded"
        if states <= {"cancelled"}:
            return "cancelled"
        if states <= TERMINAL_JOB_STATES:
            return "failed" if states == {"failed"} else "partial"
        return "running"

    def list_batches(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM batches ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self.get_batch(row["id"]) for row in rows]

    def get_job(
        self,
        job_id: str,
        *,
        include_remote_logs: bool = False,
        event_limit: int = 200,
        remote_log_limit: int = 500,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        job = self._decode(row, "job")
        if not job:
            raise NotFoundError(f"job {job_id!r} was not found")
        job["events"] = self.list_job_events(job_id, limit=event_limit)
        # Scheduler paths call ``get_job`` frequently.  Do not load a large
        # live-output page for every poll; the run-detail API explicitly opts
        # in below.  Fetch one sentinel line to make the UI pagination state
        # exact instead of guessing from a page that happens to be full.
        if include_remote_logs:
            bounded_log_limit = max(1, min(remote_log_limit, 500))
            remote_logs = self.list_remote_log_lines(
                job_id, limit=bounded_log_limit + 1
            )
            has_more = len(remote_logs) > bounded_log_limit
            if has_more:
                remote_logs = remote_logs[1:]
            job["remote_logs"] = remote_logs
            job["remote_logs_before_id"] = (
                remote_logs[0]["sequence_id"] if remote_logs else None
            )
            job["remote_logs_has_more"] = has_more
        return job

    def list_jobs(
        self,
        *,
        batch_id: str | None = None,
        account_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("batch_id", batch_id),
            ("account_id", account_id),
            ("status", status),
        ):
            if value:
                filters.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(1, min(limit, 1000)))
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs"
                + where
                + " ORDER BY created_at DESC, rowid DESC"
                + limit_clause,
                params,
            ).fetchall()
        return [self._decode(row, "job") for row in rows]  # type: ignore[misc]

    def job_state_counts(self) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def queued_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 "
                "ORDER BY created_at, rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode(row, "job") for row in rows]  # type: ignore[misc]

    def transition_job(
        self,
        job_id: str,
        from_states: set[str],
        to_state: str,
        *,
        actor: str = "scheduler",
        fields: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        changes = dict(fields or {})
        changes["status"] = to_state
        changes["updated_at"] = now
        if to_state == "submitting":
            changes.setdefault("started_at", now)
        # Recovery can safely reaffirm ``submitted`` after an app restart.
        # That must not reset the remote runtime clock used by the UI.
        if to_state == "submitted" and "submitted" not in from_states:
            changes.setdefault("remote_started_at", now)
        if to_state in TERMINAL_JOB_STATES:
            changes.setdefault("finished_at", now)
        if "result" in changes:
            changes["result_json"] = json.dumps(changes.pop("result"), separators=(",", ":"))
        assignments = ", ".join(f"{key}=?" for key in changes)
        placeholders = ",".join("?" for _ in from_states)
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id=? "  # noqa: S608
                f"AND status IN ({placeholders})",
                (*changes.values(), job_id, *sorted(from_states)),
            )
            changed = cursor.rowcount == 1
            if changed:
                self.append_audit(
                    actor,
                    f"job.{to_state}",
                    "job",
                    job_id,
                    {"quota_source": "official_kaggle_api"},
                    connection=connection,
                )
                if previous and previous["status"] != to_state:
                    self.append_job_event(
                        job_id,
                        f"Job status changed to {to_state}",
                        level="error" if to_state == "failed" else "info",
                        details={"quota_source": "official_kaggle_api"},
                        connection=connection,
                    )
        return changed

    def request_cancel(self, job_id: str, actor: str) -> tuple[dict[str, Any], str]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise NotFoundError(f"job {job_id!r} was not found")
            job = self._decode(row, "job")
            assert job is not None
            now = utc_now()
            if job["status"] == "queued":
                connection.execute(
                    "UPDATE jobs SET status='cancelled',cancel_requested=1,"
                    "quota_accounted=1,finished_at=?,updated_at=? "
                    "WHERE id=? AND status='queued'",
                    (now, now, job_id),
                )
                semantics = "queued_job_cancelled"
            elif job["status"] in ACTIVE_JOB_STATES:
                connection.execute(
                    "UPDATE jobs SET status='cancel_requested',cancel_requested=1,"
                    "remote_may_be_running=1,updated_at=? WHERE id=?",
                    (now, job_id),
                )
                semantics = "local_monitor_stop_requested"
            else:
                raise ConflictError(f"job in state {job['status']!r} cannot be cancelled")
            self.append_audit(
                actor,
                "job.cancel_requested",
                "job",
                job_id,
                {"semantics": semantics},
                connection=connection,
            )
            self.append_job_event(
                job_id,
                "Cancellation requested: " + semantics,
                level="warning",
                connection=connection,
            )
        return self.get_job(job_id), semantics

    def retry_job(self, job_id: str, actor: str, default_output_root: Path) -> dict[str, Any]:
        original = self.get_job(job_id)
        if original["status"] not in TERMINAL_JOB_STATES:
            raise ConflictError("only a terminal job can be retried")
        retry_id = new_id("job")
        now = utc_now()
        output_dir = str((default_output_root / retry_id).resolve())
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id,batch_id,account_id,experiment_name,source_dir,kernel_slug,
                    output_dir,status,attempt,retry_of_job_id,metadata_json,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,'queued',?,?,?, ?,?)
                """,
                (
                    retry_id,
                    original["batch_id"],
                    original["account_id"],
                    original["experiment_name"],
                    original["source_dir"],
                    original["kernel_slug"],
                    output_dir,
                    original["attempt"] + 1,
                    job_id,
                    json.dumps(original["metadata"], separators=(",", ":")),
                    now,
                    now,
                ),
            )
            self.append_audit(
                actor,
                "job.retried",
                "job",
                retry_id,
                {"retry_of_job_id": job_id, "account_id": original["account_id"]},
                connection=connection,
            )
            self.append_job_event(
                retry_id,
                "Retry queued on the same explicitly assigned account",
                details={"retry_of_job_id": job_id, "account_id": original["account_id"]},
                connection=connection,
            )
        return self.get_job(retry_id)

    def recover_interrupted_jobs(self) -> list[str]:
        """Preserve active remote jobs for startup reconciliation.

        Closing the desktop app only stops the local monitor; it does not stop
        a Kaggle kernel.  Do not manufacture a local ``failed`` result here.
        The service reconciles these ids with Kaggle after its scheduler and
        credential vault are ready.
        """
        now = utc_now()
        with self.connection() as connection:
            # Older schedulers marked any attempted submit as remotely
            # uncertain, even when Kaggle returned a definite client error.
            # Repair only rows whose immutable event trace proves that a
            # failure was persisted before any successful submit event.
            legacy_rejected = connection.execute(
                "SELECT jobs.id FROM jobs WHERE remote_may_be_running=1 "
                "AND (result_json IS NULL OR result_json='null') "
                "AND EXISTS (SELECT 1 FROM job_events WHERE job_id=jobs.id "
                "AND message='Job status changed to failed') "
                "AND NOT EXISTS (SELECT 1 FROM job_events WHERE job_id=jobs.id "
                "AND message='Submitted the staged kernel to Kaggle')"
            ).fetchall()
            for row in legacy_rejected:
                connection.execute(
                    "UPDATE jobs SET status='failed',remote_may_be_running=0,"
                    "error=?,finished_at=COALESCE(finished_at,?),updated_at=? WHERE id=?",
                    (
                        "legacy submit failure; Kaggle did not accept a remote kernel",
                        now,
                        now,
                        row["id"],
                    ),
                )
                self.append_audit(
                    "scheduler",
                    "job.legacy_submit_failure_repaired",
                    "job",
                    row["id"],
                    {"remote_submission_confirmed": False},
                    connection=connection,
                )
                self.append_job_event(
                    row["id"],
                    "Repaired legacy submit failure; no remote Kaggle run was accepted",
                    level="warning",
                    connection=connection,
                )
            rows = connection.execute(
                "SELECT id,status FROM jobs WHERE status IN "
                "('submitting','submitted','running','cancel_requested') "
                "OR remote_may_be_running=1 "
                "OR (status='failed' AND error=?)",
                (LEGACY_RESTART_FAILURE,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET status=CASE WHEN status IN ('submitting','failed') "
                    "THEN 'submitted' ELSE status END, remote_may_be_running=1, "
                    "error=?, finished_at=NULL, updated_at=? WHERE id=?",
                    (
                        "control plane restarted; reconciling remote Kaggle status",
                        now,
                        row["id"],
                    ),
                )
                self.append_audit(
                    "scheduler",
                    "job.recovery_pending",
                    "job",
                    row["id"],
                    {"previous_status": row["status"]},
                    connection=connection,
                )
                self.append_job_event(
                    row["id"],
                    "Control plane restarted; reconciling the remote Kaggle run",
                    level="warning",
                    connection=connection,
                )
        return [str(row["id"]) for row in rows]

    def list_audit(
        self,
        *,
        limit: int = 100,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if entity_type:
            filters.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            filters.append("entity_id=?")
            params.append(entity_id)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        params.append(max(1, min(limit, 1000)))
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT event_id,actor,action,entity_type,entity_id,details_json,created_at "
                + "FROM audit_logs"
                + where
                + " ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode(row, "audit") for row in rows]  # type: ignore[misc]

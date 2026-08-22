"""Kaggle execution adapters and per-job source staging."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import ValidationError


KERNEL_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
KAGGLE_KERNEL_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?kaggle\.com/code/([^/\s]+)/([^/?#\s]+)",
    re.IGNORECASE,
)
DEFAULT_MAX_SOURCE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_SOURCE_FILES = 10_000
DEFAULT_GPU_MACHINE_SHAPE = "NvidiaTeslaT4"
DEFAULT_TPU_MACHINE_SHAPE = "TpuV38"
RUNTIME_MANIFEST_FIELDS = {
    "accelerate",
    "cuda_capability",
    "cuda_device",
    "cuda_runtime",
    "pydantic",
    "python",
    "resolved_requirements_sha256",
    "sentence_transformers",
    "torch",
    "transformers",
}


def hidden_subprocess_kwargs(platform_name: str | None = None) -> dict[str, Any]:
    """Keep child CLIs invisible in the packaged Windows desktop app."""
    if (platform_name or os.name) != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def read_runtime_manifest(output_dir: str | Path) -> dict[str, Any] | None:
    """Read a bounded, allow-listed runtime.json from downloaded job output."""
    root = Path(output_dir).resolve()
    for candidate in sorted(root.rglob("runtime.json")):
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            continue
        if resolved.stat().st_size > 64 * 1024:
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        return {
            key: value
            for key, value in payload.items()
            if key in RUNTIME_MANIFEST_FIELDS
            and isinstance(value, (str, int, float, bool, list))
        }
    return None


class AdapterError(RuntimeError):
    pass


class LocalCommandCancelled(AdapterError):
    pass


@dataclass(frozen=True)
class RemoteStatus:
    state: str
    detail: str = ""


class KaggleAdapter(Protocol):
    def submit(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]: ...

    def status(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> RemoteStatus: ...

    def output(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]: ...

    def logs(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> str: ...

    def diagnostics(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]: ...

    def quota(
        self, env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]: ...


def normalize_kernel_slug(kaggle_username: str, value: str) -> str:
    """Return an owner/slug kernel id, rejecting attempts to switch owners."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("kernel_slug is required")
    parts = value.strip().lower().split("/")
    if len(parts) == 1:
        owner = kaggle_username.lower()
        slug = parts[0]
    elif len(parts) == 2:
        owner, slug = parts
        if owner.casefold() != kaggle_username.casefold():
            raise ValidationError(
                "kernel_slug owner must match the explicitly assigned account"
            )
    else:
        raise ValidationError("kernel_slug must be a slug or owner/slug")
    if not KERNEL_SLUG_PATTERN.fullmatch(slug):
        raise ValidationError(
            "kernel slug must use lowercase letters, digits and hyphens"
        )
    return f"{owner}/{slug}"


def stage_job_source(
    job: dict[str, Any],
    account: dict[str, Any],
    staging_root: str | Path,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_source_files: int = DEFAULT_MAX_SOURCE_FILES,
) -> dict[str, Any]:
    """Copy and validate a Kaggle source tree, then set metadata.id safely.

    The caller-supplied source is never modified. Each attempt gets its own
    private staging directory named by its immutable job id.
    """
    raw_source = Path(job["source_dir"]).expanduser()
    if raw_source.is_symlink():
        raise AdapterError("source_dir must not be a symbolic link")
    source = raw_source.resolve()
    if not source.is_dir():
        raise AdapterError(f"source_dir is not a directory: {source}")
    symlinks = [path for path in source.rglob("*") if path.is_symlink()]
    if source.is_symlink() or symlinks:
        raise AdapterError("source_dir must not contain symbolic links")
    files = [path for path in source.rglob("*") if path.is_file()]
    if len(files) > max_source_files:
        raise AdapterError(
            f"source_dir contains too many files ({len(files)} > {max_source_files})"
        )
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes > max_source_bytes:
        raise AdapterError(
            f"source_dir is too large ({total_bytes} > {max_source_bytes} bytes)"
        )
    metadata_path = source / "kernel-metadata.json"
    if not metadata_path.is_file():
        raise AdapterError("source_dir must contain kernel-metadata.json")
    code_files = [
        path
        for path in source.iterdir()
        if path.is_file() and path.suffix.lower() in {".py", ".ipynb"}
    ]
    if len(code_files) != 1:
        raise AdapterError(
            "source_dir must contain exactly one top-level .py or .ipynb file"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"invalid kernel-metadata.json: {exc}") from exc
    if not isinstance(metadata, dict):
        raise AdapterError("kernel-metadata.json must contain a JSON object")

    expected_id = normalize_kernel_slug(account["kaggle_username"], job["kernel_slug"])
    # Kaggle gives legacy numeric `id_no` precedence over `id`. Removing it is
    # essential: otherwise a copied source can target another kernel despite
    # the account-safe id rewrite below.
    metadata.pop("id_no", None)
    metadata["id"] = expected_id
    # Kaggle derives the slug of a newly-created kernel from its title. Keep
    # title and id aligned so the CLI does not silently create a different
    # remote slug and then fail status polling against the requested id.
    metadata["title"] = expected_id.split("/", 1)[1]
    metadata["code_file"] = code_files[0].name
    accelerator = str(job.get("metadata", {}).get("accelerator", "auto"))
    machine_shape = str(job.get("metadata", {}).get("machine_shape", ""))
    if accelerator == "gpu":
        metadata["enable_gpu"] = True
        metadata["enable_tpu"] = False
        metadata["machine_shape"] = machine_shape or DEFAULT_GPU_MACHINE_SHAPE
    elif accelerator == "tpu":
        metadata["enable_gpu"] = False
        metadata["enable_tpu"] = True
        metadata["machine_shape"] = machine_shape or DEFAULT_TPU_MACHINE_SHAPE
    elif accelerator == "cpu":
        metadata["enable_gpu"] = False
        metadata["enable_tpu"] = False
        metadata.pop("machine_shape", None)

    root = Path(staging_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / job["id"]).resolve()
    if destination.parent != root:
        raise AdapterError("invalid job id for staging")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    (destination / "kernel-metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    staged_job = dict(job)
    staged_job["source_dir"] = str(destination)
    staged_job["kernel_slug"] = expected_id
    return staged_job


class KaggleCliAdapter:
    def __init__(
        self,
        executable: str | None = None,
        command_poll_seconds: float = 0.1,
        live_log_capture_seconds: float = 8.0,
    ):
        self.executable = executable or os.environ.get("KCP_KAGGLE_EXECUTABLE", "kaggle")
        self.command_poll_seconds = command_poll_seconds
        self.live_log_capture_seconds = max(0.1, live_log_capture_seconds)

    def _run(
        self,
        args: list[str],
        env: dict[str, str],
        cancel_event: threading.Event,
    ) -> str:
        if cancel_event.is_set():
            raise LocalCommandCancelled("local Kaggle CLI command was cancelled")
        # A temporary file prevents the classic PIPE deadlock when the CLI
        # emits more than an OS pipe buffer while the parent is polling.
        with tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        ) as output_file:
            command_env = dict(env)
            command_env["PYTHONIOENCODING"] = "utf-8"
            command_env["PYTHONUTF8"] = "1"
            command_env["PYTHONUNBUFFERED"] = "1"
            try:
                process = subprocess.Popen(
                    [self.executable, *args],
                    env=command_env,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    **hidden_subprocess_kwargs(),
                )
            except OSError as exc:
                raise AdapterError(f"could not start Kaggle CLI: {exc}") from exc
            while process.poll() is None:
                if cancel_event.wait(self.command_poll_seconds):
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise LocalCommandCancelled("local Kaggle CLI command was cancelled")
            output_file.seek(0)
            output = output_file.read().strip()
            if process.returncode:
                raise AdapterError(
                    f"Kaggle CLI exited with {process.returncode}: {output[-2000:]}"
                )
            return output

    def _run_follow_snapshot(
        self,
        args: list[str],
        env: dict[str, str],
        cancel_event: threading.Event,
    ) -> str:
        """Capture a bounded snapshot from Kaggle's live SSE log stream.

        The non-following CLI command reads only the persisted log blob, which
        remains empty while a kernel is running. ``--follow`` exposes the live
        stream and replays it from the beginning on each connection. We keep
        the connection open briefly, then stop only our local CLI process; the
        remote Kaggle kernel is unaffected.
        """
        if cancel_event.is_set():
            raise LocalCommandCancelled("local Kaggle CLI command was cancelled")
        command_env = dict(env)
        command_env["PYTHONIOENCODING"] = "utf-8"
        command_env["PYTHONUTF8"] = "1"
        # `kaggle kernels logs --follow` is a Python console program. Its
        # stdout must not be buffered when Control Plane captures a bounded
        # snapshot instead of attaching a terminal.
        command_env["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen(
                [self.executable, *args],
                env=command_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=0,
                shell=False,
                **hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            raise AdapterError(f"could not start Kaggle CLI: {exc}") from exc

        def stop_follow_process() -> None:
            """Stop only the local CLI process tree; Kaggle keeps the kernel running.

            On Windows ``kaggle.exe`` launches a Python child that can retain
            the stdout pipe after its launcher receives ``terminate``.  A
            process-tree stop is necessary so a bounded live-log snapshot
            cannot pin the scheduler worker indefinitely.
            """
            if process.poll() is not None:
                return
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=3,
                        **hidden_subprocess_kwargs(),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

        # Drain continuously. A temporary output file only exposes data after
        # the CLI flushes and can lose a partial live line when we stop its
        # local `--follow` process. Character reads preserve exactly what the
        # Kaggle stream has emitted without risking a pipe-buffer deadlock.
        output_chunks: list[str] = []
        output_lock = threading.Lock()

        def drain_stdout() -> None:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(1)
                if not chunk:
                    return
                with output_lock:
                    output_chunks.append(chunk)

        reader = threading.Thread(target=drain_stdout, name="kcp-live-log-reader", daemon=True)
        reader.start()
        deadline = time.monotonic() + self.live_log_capture_seconds
        stopped_after_snapshot = False
        try:
            while process.poll() is None and time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                if cancel_event.wait(min(self.command_poll_seconds, remaining)):
                    stop_follow_process()
                    raise LocalCommandCancelled("local Kaggle CLI command was cancelled")
            if process.poll() is None:
                stopped_after_snapshot = True
                stop_follow_process()
        finally:
            # Some Windows console process trees retain the inherited write
            # end briefly after the CLI parent exits. Never let that keep the
            # scheduler worker blocked indefinitely. Do not close a pipe while
            # the reader is blocked in a Windows read: TextIOWrapper.close()
            # can itself wait for the inherited writer. The daemon owns the
            # handle until EOF in that rare case.
            reader.join(timeout=0.5)
            if not reader.is_alive() and process.stdout is not None:
                process.stdout.close()

        with output_lock:
            output = "".join(output_chunks).strip()
        if not stopped_after_snapshot and process.returncode:
            raise AdapterError(
                f"Kaggle CLI exited with {process.returncode}: {output[-2000:]}"
            )
        return output

    def submit(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        args = ["kernels", "push", "-p", job["source_dir"]]
        machine_shape = str(job.get("metadata", {}).get("machine_shape", ""))
        if machine_shape:
            args.extend(["--accelerator", machine_shape])
        output = self._run(
            args, env, cancel_event
        )
        expected_owner, expected_slug = job["kernel_slug"].split("/", 1)
        match = KAGGLE_KERNEL_URL_PATTERN.search(output)
        if match:
            actual_owner, actual_slug = match.groups()
            if (
                actual_owner.casefold() != expected_owner.casefold()
                or actual_slug.casefold() != expected_slug.casefold()
            ):
                raise AdapterError(
                    "Kaggle submitted the kernel as "
                    f"{actual_owner}/{actual_slug}, but the assigned account expects "
                    f"{expected_owner}/{expected_slug}. Verify that the credential belongs "
                    "to the registered Kaggle username and retry with a new kernel slug."
                )
        return {"kernel_slug": job["kernel_slug"], "cli_message": output[-2000:]}

    def status(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> RemoteStatus:
        output = self._run(
            ["kernels", "status", job["kernel_slug"]], env, cancel_event
        )
        normalized = output.casefold()
        if any(word in normalized for word in ("error", "failed", "failure")):
            state = "failed"
        elif any(word in normalized for word in ("complete", "success")):
            state = "complete"
        elif "cancel" in normalized:
            state = "cancelled"
        elif any(word in normalized for word in ("running", "active")):
            state = "running"
        else:
            state = "queued"
        return RemoteStatus(state, output[-2000:])

    def output(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        output_dir = Path(job["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        message = self._run(
            [
                "kernels",
                "output",
                job["kernel_slug"],
                "-p",
                str(output_dir),
                "--force",
            ],
            env,
            cancel_event,
        )
        files = [
            str(path.relative_to(output_dir))
            for path in output_dir.rglob("*")
            if path.is_file()
        ][:1000]
        result = {
            "output_dir": str(output_dir),
            "files": sorted(files),
            "cli_message": message[-2000:],
        }
        runtime = read_runtime_manifest(output_dir)
        if runtime:
            result["runtime"] = runtime
        return result

    def logs(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> str:
        return self._run_follow_snapshot(
            ["kernels", "logs", "--follow", job["kernel_slug"]], env, cancel_event
        )

    def diagnostics(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        output_dir = Path(job["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        message = self._run(
            ["kernels", "logs", job["kernel_slug"]], env, cancel_event
        )
        if not message:
            raise AdapterError("Kaggle returned an empty kernel diagnostic log")
        target = output_dir / f"{job['kernel_slug'].split('/', 1)[-1]}.log"
        target.write_text(message + "\n", encoding="utf-8", newline="\n")
        return {
            "output_dir": str(output_dir),
            "files": [target.name],
            "cli_message": "Downloaded Kaggle kernel execution log",
        }

    def quota(
        self, env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        output = self._run(["quota", "--format", "json"], env, cancel_event)
        try:
            rows = json.loads(output)
        except json.JSONDecodeError as exc:
            raise AdapterError("Kaggle returned invalid quota JSON") from exc
        if not isinstance(rows, list):
            raise AdapterError("Kaggle returned an unexpected quota response")
        resources: dict[str, dict[str, Any]] = {}
        refresh_at: str | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            resource = str(row.get("resource", "")).lower()
            if resource not in {"gpu", "tpu"}:
                continue
            try:
                def hours(name: str) -> float:
                    return float(str(row[name]).removesuffix("h"))
                resources[resource] = {
                    "used_hours": hours("used"),
                    "remaining_hours": hours("remaining"),
                    "total_hours": hours("total"),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise AdapterError(f"Kaggle returned invalid {resource.upper()} quota") from exc
            refresh_at = str(row.get("refreshAt") or refresh_at or "") or None
        if not resources:
            raise AdapterError("Kaggle returned no GPU or TPU quota information")
        return {"source": "kaggle", "refresh_at": refresh_at, "resources": resources}


class FakeKaggleAdapter:
    """Deterministic adapter for demos and end-to-end tests.

    Job metadata may set fake_polls, fake_outcome and fake_result. This adapter
    still exercises credential isolation, source staging and scheduler logic.
    """

    def __init__(
        self,
        poll_delay_seconds: float = 0.01,
        quota_result: dict[str, Any] | None = None,
    ):
        self.poll_delay_seconds = poll_delay_seconds
        self._lock = threading.Lock()
        self._polls: dict[str, int] = {}
        self._log_polls: dict[str, int] = {}
        self.max_in_flight = 0
        self._in_flight = 0
        self._active_remote_jobs: set[str] = set()
        self.submitted_env_markers: dict[str, dict[str, str]] = {}
        self.quota_result = quota_result or {
            "source": "kaggle",
            "refresh_at": "2099-01-01T00:00:00",
            "resources": {
                "gpu": {"used_hours": 1.0, "remaining_hours": 29.0, "total_hours": 30.0},
                "tpu": {"used_hours": 2.0, "remaining_hours": 18.0, "total_hours": 20.0},
            },
        }

    def submit(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        if cancel_event.is_set():
            raise LocalCommandCancelled("fake submit cancelled")
        with self._lock:
            self._in_flight += 1
            self._active_remote_jobs.add(job["id"])
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.submitted_env_markers[job["id"]] = {
                "KAGGLE_USERNAME": env.get("KAGGLE_USERNAME", ""),
                "KAGGLE_CONFIG_DIR": env.get("KAGGLE_CONFIG_DIR", ""),
                "credential_kind": (
                    "token" if env.get("KAGGLE_API_TOKEN") else "legacy_key"
                ),
            }

        delay = float(job["metadata"].get("fake_submit_delay", self.poll_delay_seconds))
        if cancel_event.wait(delay):
            with self._lock:
                self._in_flight -= 1
                self._active_remote_jobs.discard(job["id"])
            raise LocalCommandCancelled("fake submit cancelled")
        return {"kernel_slug": job["kernel_slug"], "adapter": "fake"}

    def quota(
        self, env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        if cancel_event.is_set():
            raise LocalCommandCancelled("fake quota sync cancelled")
        return json.loads(json.dumps(self.quota_result))

    def status(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> RemoteStatus:
        if cancel_event.wait(self.poll_delay_seconds):
            raise LocalCommandCancelled("fake monitor cancelled")
        with self._lock:
            count = self._polls.get(job["id"], 0) + 1
            self._polls[job["id"]] = count
        needed = int(job["metadata"].get("fake_polls", 2))
        if count < needed:
            return RemoteStatus("running", f"fake poll {count}/{needed}")
        outcome = str(job["metadata"].get("fake_outcome", "complete"))
        if outcome == "failed":
            with self._lock:
                if job["id"] in self._active_remote_jobs:
                    self._active_remote_jobs.remove(job["id"])
                    self._in_flight -= 1
            return RemoteStatus("failed", "fake remote failure")
        if outcome == "cancelled":
            with self._lock:
                if job["id"] in self._active_remote_jobs:
                    self._active_remote_jobs.remove(job["id"])
                    self._in_flight -= 1
            return RemoteStatus("cancelled", "fake remote cancellation")
        return RemoteStatus("complete", "fake complete")

    def output(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        if cancel_event.is_set():
            raise LocalCommandCancelled("fake output cancelled")
        with self._lock:
            if job["id"] in self._active_remote_jobs:
                self._active_remote_jobs.remove(job["id"])
                self._in_flight -= 1
        output_dir = Path(job["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if str(job["metadata"].get("fake_outcome", "complete")) == "failed":
            log_path = output_dir / f"{job['kernel_slug'].split('/', 1)[-1]}.log"
            log_path.write_text(
                str(
                    job["metadata"].get(
                        "fake_remote_log", "Traceback: fake remote kernel failure"
                    )
                ),
                encoding="utf-8",
            )
            return {
                "output_dir": str(output_dir),
                "files": [log_path.name],
                "cli_message": "Downloaded failed kernel diagnostics",
            }
        payload = job["metadata"].get("fake_result", {"score": 0.5})
        result_path = output_dir / "fake-result.json"
        result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return {
            "output_dir": str(output_dir),
            "files": [result_path.name],
            "fake_result": payload,
        }

    def logs(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> str:
        if cancel_event.is_set():
            raise LocalCommandCancelled("fake log sync cancelled")
        configured = job["metadata"].get("fake_live_logs", "")
        if not isinstance(configured, list):
            return str(configured)
        with self._lock:
            count = self._log_polls.get(job["id"], 0) + 1
            self._log_polls[job["id"]] = count
        return "\n".join(str(line) for line in configured[:count])

    def diagnostics(
        self, job: dict[str, Any], env: dict[str, str], cancel_event: threading.Event
    ) -> dict[str, Any]:
        if cancel_event.is_set():
            raise LocalCommandCancelled("fake diagnostics cancelled")
        output_dir = Path(job["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / f"{job['kernel_slug'].split('/', 1)[-1]}.log"
        log_path.write_text(
            str(
                job["metadata"].get(
                    "fake_remote_log", "Traceback: fake remote kernel failure"
                )
            ),
            encoding="utf-8",
        )
        return {
            "output_dir": str(output_dir),
            "files": [log_path.name],
            "cli_message": "Downloaded failed kernel diagnostics",
        }

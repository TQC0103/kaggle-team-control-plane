"""One-shot local deployment, after a validated research chain and idle KCP.

No scheduler mutation, credential access, GPU submission, or Windows scheduled
task is used. Missing API evidence fails closed. Log contains safe states only.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import urlopen


TERMINAL = {"succeeded", "failed", "cancelled"}


def chain_finished(state):
    runs = state.get("authorized_run_ids", [])
    if state.get("status") != "ANALYZED" or not runs:
        return False
    return all(any(j.get("run") == run and j.get("stop") == 15000
                   and j.get("artifact_status") == "VALIDATED"
                   for j in state.get("jobs", [])) for run in runs)


def idle_snapshot(accounts, jobs, operations):
    if not accounts:
        return False
    if any(a.get("active_runs", 0) or a.get("remote_active_runs", 0)
           or a.get("remote_reconciliation_required") for a in accounts):
        return False
    if any(j.get("status") not in TERMINAL or j.get("remote_may_be_running") for j in jobs):
        return False
    return all(op.get("status") in TERMINAL for op in operations)


def api(path):
    with urlopen("http://127.0.0.1:8765/api" + path, timeout=10) as response:
        return json.load(response)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_release(root, manifest):
    root = root.resolve()
    files = manifest.get("release_files_sha256", {})
    if "KaggleControlPlane.exe" not in files:
        raise ValueError("Release manifest incomplete")
    for relative, expected in files.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha(path) != expected:
            raise ValueError("Release file validation failed")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def close_exact_app(exe):
    # Graceful window close only. No force kill and no unrelated process stop.
    command = ("$ErrorActionPreference='Stop'; $target=" + ps_literal(exe) + "; "
               "$apps=@(Get-Process -Name KaggleControlPlane -ErrorAction SilentlyContinue | "
               "Where-Object { $_.Path -eq $target }); "
               "foreach($app in $apps) { if(-not $app.CloseMainWindow()) "
               "{ throw 'Cannot close installed app gracefully' }; "
               "if(-not $app.WaitForExit(60000)) { throw 'App is still running' } }")
    subprocess.run(["powershell", "-NoProfile", "-Command", command],
                   check=True, capture_output=True, timeout=70, creationflags=0x08000000)


def launch_hidden(exe):
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Start-Process -FilePath " + ps_literal(exe) + " -WindowStyle Hidden"],
                   check=True, capture_output=True, timeout=15, creationflags=0x08000000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wait-for-state", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--max-wait-hours", type=float, default=72)
    args = parser.parse_args()
    import msvcrt
    lock = args.status_file.with_suffix(".lock").open("a+b")
    lock.seek(0); lock.write(b"0"); lock.flush(); lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    def record(state, **extra):
        value = dict(state=state, pid=os.getpid(), updated_at=datetime.now(timezone.utc).isoformat(), **extra)
        temporary = args.status_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf8")
        temporary.replace(args.status_file)
    manifest = json.loads(args.manifest.read_text(encoding="utf8"))
    verify_release(args.release_root, manifest)
    install = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "KaggleControlPlane"
    exe = install / "KaggleControlPlane.exe"
    deadline = time.monotonic() + args.max_wait_hours * 3600
    def ready():
        state = json.loads(args.wait_for_state.read_text(encoding="utf8"))
        if not chain_finished(state):
            return False
        # No limit: include every job, including older remote uncertainties.
        return idle_snapshot(api("/accounts")["accounts"],
                             api("/jobs?summary=true")["jobs"],
                             api("/datasets/operations")["operations"])
    try:
        record("WAITING_FOR_VALIDATED_CHAIN_AND_IDLE", version=manifest["version"])
        while time.monotonic() < deadline:
            try:
                can_install = ready()
            except (OSError, ValueError, KeyError):
                can_install = False
            if can_install:
                time.sleep(10)
                try:
                    if ready():
                        break
                except (OSError, ValueError, KeyError):
                    pass
            time.sleep(60)
        else:
            record("EXPIRED_WITHOUT_INSTALLING"); return
        verify_release(args.release_root, manifest)
        record("INSTALLING_AFTER_IDLE_CONFIRMED", version=manifest["version"])
        close_exact_app(exe)
        # Only binaries are copied. DB, DPAPI vault and configuration are outside
        # this directory and are never read or modified by this helper.
        backup = args.release_root.parent / "installed-before-refresh-fix"
        shutil.copytree(install, backup, dirs_exist_ok=True)
        try:
            shutil.copytree(args.release_root, install, dirs_exist_ok=True)
            verify_release(install, manifest)
            launch_hidden(exe)
        except Exception:
            shutil.copytree(backup, install, dirs_exist_ok=True)
            launch_hidden(exe)
            raise
        record("INSTALLED", version=manifest["version"], installed_exe_sha256=sha(exe))
        for _ in range(20):
            try:
                health = api("/health")
                if health.get("version") == manifest["version"]:
                    record("ACTIVE_VERIFIED", version=manifest["version"], installed_exe_sha256=sha(exe)); return
            except (OSError, ValueError):
                pass
            time.sleep(3)
        record("INSTALLED_RUNTIME_VERIFICATION_PENDING", version=manifest["version"], installed_exe_sha256=sha(exe))
    except Exception as exc:
        record("STOPPED_WITHOUT_FURTHER_ACTION", error_type=type(exc).__name__)


if __name__ == "__main__":
    main()

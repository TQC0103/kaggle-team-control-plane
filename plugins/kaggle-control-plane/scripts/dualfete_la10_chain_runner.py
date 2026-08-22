#!/usr/bin/env python3
"""Restart-safe, sequential Kaggle Control Plane chain for DualFete LA 10%.

It never accesses Kaggle credentials.  It only calls the local Control Plane
API and creates a successor after the preceding job is marked ``succeeded``
(which means its output has been downloaded).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API = "http://127.0.0.1:8765"
ACCOUNT_ID = "acct_d321057bf0954d048b448711e0efed7f"
EXPERIMENTS = Path(r"C:\Users\ASUS\Documents\Codex\2026-08-13\t\experiments")
TEMPLATE = EXPERIMENTS / "dualfete_la10_official_chunk_03_2200_v1"
DATA_KERNEL = "tqc0103/dualfete-la10-smoke-v4/1"
MAX_ITERATIONS = 15_000
CHUNK_SIZE = 1_000
POLL_SECONDS = 90


def state_path() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "KaggleControlPlane" / "data" / "runtime" / "chains" / "dualfete_la10_15k.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {
            "format": 1,
            "protocol": "repo_faithful_la10_resumable_chain",
            "max_iterations": MAX_ITERATIONS,
            "chunk_size": CHUNK_SIZE,
            "data_kernel_output": DATA_KERNEL,
            "current_job_id": "job_39a18b8482d343f180f6246270a27100",
            "current_kernel": "tqc0103/dualfete-la10-official-chunk-03-2200-v1",
            "current_target_iteration": 2200,
            "status": "watching",
            "history": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("protocol") != "repo_faithful_la10_resumable_chain":
        raise RuntimeError("unexpected chain state protocol")
    return payload


def request_json(path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API}{path}", data=body, method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def require_downloaded_checkpoint(job: dict[str, Any]) -> None:
    output_dir = Path(str(job["output_dir"]))
    candidates = list(output_dir.rglob("resume_latest.pth"))
    metric_files = list(output_dir.rglob("metrics_history.json"))
    if len(candidates) != 1 or not metric_files:
        raise RuntimeError(
            f"successful predecessor artifact is incomplete: checkpoints={candidates}, metrics={metric_files}"
        )
    if candidates[0].stat().st_size <= 0:
        raise RuntimeError("predecessor resume checkpoint is empty")


def successor_source(state: dict[str, Any], target: int) -> tuple[Path, str]:
    sequence = len(state["history"]) + 4
    slug = f"dualfete-la10-official-chain-{sequence:02d}-{target}-v1"
    source = EXPERIMENTS / f"dualfete_la10_official_chain_{sequence:02d}_{target}_v1"
    kernel = f"tqc0103/{slug}"
    if source.exists():
        metadata = json.loads((source / "kernel-metadata.json").read_text(encoding="utf-8"))
        if metadata.get("id") != kernel:
            raise RuntimeError(f"refusing to reuse unexpected source bundle: {source}")
        return source, slug

    shutil.copytree(TEMPLATE, source)
    run_path = source / "run.py"
    run = run_path.read_text(encoding="utf-8")
    run, count = re.subn(r"(?m)^STOP_AFTER = \d+$", f"STOP_AFTER = {target}", run, count=1)
    if count != 1:
        raise RuntimeError("unable to set chunk boundary")
    run = run.replace("resumable chunk stop=2200", f"resumable chunk stop={target}")
    if target == MAX_ITERATIONS:
        anchor = "    sh(cmd, repo)\n    shutil.copytree(repo / \"model\", ROOT / \"model\", dirs_exist_ok=True)"
        final_eval = """    sh(cmd, repo)
    sh([py, "code/test_performance.py", "--dataset", "LA", "--root_path", data_dir,
        "--exp", "l8_dualfete/exp_dualfete", "--model", "vnet", "--gpu", "0",
        "--labeled_num", "8"], repo)
    shutil.copytree(repo / "model", ROOT / "model", dirs_exist_ok=True)"""
        if anchor not in run:
            raise RuntimeError("unable to add official final evaluation")
        run = run.replace(anchor, final_eval, 1)
    cleanup_anchor = '    shutil.copytree(repo / "model", ROOT / "model", dirs_exist_ok=True)'
    cleanup = '''    # The venv is job-local and no longer needed after training/evaluation.
    # Do not publish it as a notebook output: it delays durable checkpoint
    # handoff by thousands of irrelevant files.
    if VENV.exists():
        shutil.rmtree(VENV)
    shutil.copytree(repo / "model", ROOT / "model", dirs_exist_ok=True)'''
    if cleanup_anchor not in run:
        raise RuntimeError("unable to add output cleanup")
    run = run.replace(cleanup_anchor, cleanup, 1)
    run_path.write_text(run, encoding="utf-8")

    metadata_path = source / "kernel-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["id"] = kernel
    metadata["title"] = f"DualFete LA 10pct automatic chain to {target}"
    metadata["kernel_sources"] = [f"{state['current_kernel']}/1", DATA_KERNEL]
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return source, slug


def submit_successor(state: dict[str, Any], predecessor: dict[str, Any]) -> dict[str, Any]:
    require_downloaded_checkpoint(predecessor)
    start = int(state["current_target_iteration"])
    target = min(start + CHUNK_SIZE, MAX_ITERATIONS)
    source, slug = successor_source(state, target)
    response = request_json(
        "/api/batches", "POST",
        {
            "name": f"DualFete LA10 automatic chain {start}-{target}",
            "jobs": [{
                "account_id": ACCOUNT_ID,
                "experiment_name": f"DualFete LA10 official {start}-{target}",
                "source_dir": str(source),
                "kernel_slug": slug,
                "metadata": {
                    "accelerator": "gpu",
                    "machine_shape": "NvidiaTeslaT4",
                    "protocol": "repo_faithful_la10_resumable_chain",
                    "start_iteration": start,
                    "stop_after_iterations": target,
                    "max_iterations": MAX_ITERATIONS,
                    "checkpoint_frequency": 4,
                    "previous_kernel_output": f"{state['current_kernel']}/1",
                    "data_kernel_output": DATA_KERNEL,
                    "base_environment_modified": False,
                    "uses_repo_single_gpu": True,
                },
            }],
        },
    )
    job = response["batch"]["jobs"][0]
    state["history"].append({
        "job_id": predecessor["id"], "kernel": state["current_kernel"],
        "target_iteration": start, "status": "succeeded",
    })
    state.update({
        "current_job_id": job["id"], "current_kernel": job["kernel_slug"],
        "current_target_iteration": target, "status": "watching",
    })
    atomic_write_json(state_path(), state)
    print(json.dumps({"event": "submitted_successor", "job": job["id"], "target": target}), flush=True)
    return state


def run_once() -> int:
    state = load_state()
    atomic_write_json(state_path(), state)
    job = request_json(f"/api/jobs/{state['current_job_id']}")["job"]
    status = job["status"]
    print(json.dumps({"job": job["id"], "status": status, "target": state["current_target_iteration"]}), flush=True)
    if status == "succeeded":
        if int(state["current_target_iteration"]) >= MAX_ITERATIONS:
            require_downloaded_checkpoint(job)
            state["status"] = "completed"
            state["history"].append({
                "job_id": job["id"], "kernel": state["current_kernel"],
                "target_iteration": MAX_ITERATIONS, "status": "succeeded",
            })
            atomic_write_json(state_path(), state)
            return 0
        submit_successor(state, job)
    elif status in {"failed", "cancelled"}:
        state["status"] = "blocked"
        state["blocked_job_status"] = status
        state["blocked_job_error"] = job.get("error")
        atomic_write_json(state_path(), state)
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    while True:
        try:
            code = run_once()
        except (OSError, ValueError, KeyError, urllib.error.URLError, urllib.error.HTTPError) as error:
            print(json.dumps({"event": "transient_error", "error": str(error)}), flush=True)
            code = 1
        if arguments.once or code in {0, 2}:
            return code
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())

"""Dependency-free stdio MCP bridge for the local Kaggle Control Plane."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = os.environ.get("KCP_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
PROTOCOL_VERSION = "2025-06-18"


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "kcp_status",
        "description": "Check whether Control Plane is reachable and summarize accounts and jobs.",
        "inputSchema": _schema(),
    },
    {
        "name": "kcp_open_app",
        "description": "Open the installed Kaggle Control Plane desktop application.",
        "inputSchema": _schema(),
    },
    {
        "name": "kcp_list_accounts",
        "description": "List Control Plane account IDs, states, activity, and accelerator quota. Never returns credentials.",
        "inputSchema": _schema(),
    },
    {
        "name": "kcp_list_jobs",
        "description": "List recent Control Plane jobs, optionally filtered by status.",
        "inputSchema": _schema(
            {
                "status": {
                    "type": "string",
                    "enum": [
                        "queued",
                        "submitting",
                        "submitted",
                        "running",
                        "cancel_requested",
                        "succeeded",
                        "failed",
                        "cancelled",
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            }
        ),
    },
    {
        "name": "kcp_get_job",
        "description": "Get one Control Plane job, normalized accelerator/machine shape, elapsed runtime, resolved runtime manifest when available, and its recent event trace.",
        "inputSchema": _schema({"job_id": {"type": "string", "minLength": 1}}, ["job_id"]),
    },
    {
        "name": "kcp_submit_batch",
        "description": "Submit one bounded batch of at most ten Kaggle jobs with an explicit accelerator shape. GPU defaults to NvidiaTeslaT4. This consumes remote quota.",
        "inputSchema": _schema(
            {
                "batch_name": {"type": "string", "minLength": 1, "maxLength": 120},
                "jobs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": _schema(
                        {
                            "account_id": {"type": "string", "minLength": 1},
                            "experiment_name": {"type": "string", "minLength": 1, "maxLength": 120},
                            "source_dir": {"type": "string", "minLength": 1},
                            "kernel_slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
                            "accelerator": {"type": "string", "enum": ["gpu", "tpu", "cpu"]},
                            "machine_shape": {
                                "type": "string",
                                "enum": ["NvidiaTeslaT4", "TpuV38"],
                                "description": "Exact Kaggle machine shape. Use NvidiaTeslaT4 for GPU or TpuV38 for TPU; omit for CPU.",
                            },
                        },
                        ["account_id", "experiment_name", "source_dir", "kernel_slug", "accelerator"],
                    ),
                },
            },
            ["batch_name", "jobs"],
        ),
    },
    {
        "name": "kcp_job_action",
        "description": "Cancel, retry, or request result metadata for one job. Cancel and retry change remote state.",
        "inputSchema": _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "action": {"type": "string", "enum": ["cancel", "retry", "result"]},
            },
            ["job_id", "action"],
        ),
    },
    {
        "name": "kcp_job_events",
        "description": "Read a bounded page of job events.",
        "inputSchema": _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                "before_id": {"type": "integer", "minimum": 1},
            },
            ["job_id"],
        ),
    },
    {
        "name": "kcp_download_artifact",
        "description": "Download a job result ZIP or log file to an explicit existing directory.",
        "inputSchema": _schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "artifact": {"type": "string", "enum": ["result", "logs"]},
                "destination_dir": {"type": "string", "minLength": 1},
            },
            ["job_id", "artifact", "destination_dir"],
        ),
    },
]


def _request(path: str, method: str = "GET", body: Any | None = None, timeout: float = 8.0) -> tuple[Any, dict[str, str]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Kaggle Control Plane API is offline. Open the desktop app with kcp_open_app, wait for startup, and retry."
        ) from exc
    if not raw:
        return {}, headers
    try:
        return json.loads(raw), headers
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace"), headers


def _safe(value: Any) -> Any:
    blocked = {"credential", "credentials", "token", "access_token", "refresh_token", "api_key", "key"}
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if key.lower() in blocked else _safe(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _items(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _open_app() -> dict[str, Any]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    executable = local_app_data / "Programs" / "KaggleControlPlane" / "KaggleControlPlane.exe"
    if not executable.is_file():
        raise RuntimeError(f"Kaggle Control Plane is not installed at {executable}")
    subprocess.Popen([str(executable)], close_fds=True)
    return {"opened": True, "executable": str(executable), "note": "Wait for the desktop UI to finish starting before retrying."}


def _source_job(job: dict[str, Any]) -> dict[str, Any]:
    source = Path(job["source_dir"]).expanduser().resolve()
    if not source.is_dir() or not (source / "kernel-metadata.json").is_file():
        raise ValueError(f"source_dir must be an existing notebook folder containing kernel-metadata.json: {source}")
    home = Path.home().resolve()
    if source == home or source == home.parent or source == Path(source.anchor):
        raise ValueError("source_dir is too broad")
    cleaned = dict(job)
    cleaned["source_dir"] = str(source)
    accelerator = cleaned.pop("accelerator")
    machine_shape = cleaned.pop("machine_shape", None)
    if accelerator == "gpu":
        machine_shape = machine_shape or "NvidiaTeslaT4"
        if machine_shape != "NvidiaTeslaT4":
            raise ValueError("GPU jobs currently require machine_shape NvidiaTeslaT4")
    elif accelerator == "tpu":
        machine_shape = machine_shape or "TpuV38"
        if machine_shape != "TpuV38":
            raise ValueError("TPU jobs currently require machine_shape TpuV38")
    elif machine_shape is not None:
        raise ValueError("CPU jobs must not specify machine_shape")
    cleaned["metadata"] = {"accelerator": accelerator}
    if machine_shape is not None:
        cleaned["metadata"]["machine_shape"] = machine_shape
    return cleaned


def _download(path: str, destination_dir: str, fallback_name: str) -> dict[str, Any]:
    destination = Path(destination_dir).expanduser().resolve()
    if not destination.is_dir():
        raise ValueError("destination_dir must already exist")
    request = urllib.request.Request(f"{BASE_URL}{path}", headers={"Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            disposition = response.headers.get("Content-Disposition", "")
            filename = fallback_name
            marker = "filename="
            if marker in disposition:
                filename = disposition.split(marker, 1)[1].strip().strip('"')
            filename = Path(filename).name
            data = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Could not download artifact from Kaggle Control Plane") from exc
    target = destination / filename
    target.write_bytes(data)
    return {"path": str(target), "bytes": len(data)}


def _call(name: str, arguments: dict[str, Any]) -> Any:
    if name == "kcp_open_app":
        return _open_app()
    if name == "kcp_list_accounts":
        payload, _ = _request("/api/accounts")
        return _safe(payload)
    if name == "kcp_list_jobs":
        query = {
            "limit": int(arguments.get("limit", 20)),
            "summary": 1,
        }
        if arguments.get("status"):
            query["status"] = arguments["status"]
        payload, _ = _request(f"/api/jobs?{urllib.parse.urlencode(query)}")
        jobs = _items(payload, "jobs", "runs", "items", "data")
        return _safe(jobs)
    if name == "kcp_status":
        accounts_payload, _ = _request("/api/accounts")
        stats_payload, _ = _request("/api/jobs/stats")
        accounts = _items(accounts_payload, "accounts", "items", "data")
        states = stats_payload.get("states", {}) if isinstance(stats_payload, dict) else {}
        return {"online": True, "base_url": BASE_URL, "account_count": len(accounts), "job_states": states}
    if name == "kcp_get_job":
        job_id = urllib.parse.quote(arguments["job_id"], safe="")
        query = urllib.parse.urlencode(
            {"include_remote_logs": 0, "event_limit": 100}
        )
        payload, _ = _request(f"/api/jobs/{job_id}?{query}")
        return _safe(payload)
    if name == "kcp_submit_batch":
        jobs = [_source_job(job) for job in arguments["jobs"]]
        payload, _ = _request("/api/batches", "POST", {"name": arguments["batch_name"], "jobs": jobs}, timeout=30)
        return _safe(payload)
    if name == "kcp_job_action":
        job_id = urllib.parse.quote(arguments["job_id"], safe="")
        action = arguments["action"]
        method = "GET" if action == "result" else "POST"
        payload, _ = _request(f"/api/jobs/{job_id}/{action}", method, timeout=30)
        return _safe(payload)
    if name == "kcp_job_events":
        job_id = urllib.parse.quote(arguments["job_id"], safe="")
        query = {"limit": int(arguments.get("limit", 100))}
        if "before_id" in arguments:
            query["before_id"] = int(arguments["before_id"])
        payload, _ = _request(f"/api/jobs/{job_id}/events?{urllib.parse.urlencode(query)}")
        return _safe(payload)
    if name == "kcp_download_artifact":
        job_id = urllib.parse.quote(arguments["job_id"], safe="")
        artifact = arguments["artifact"]
        suffix = "zip" if artifact == "result" else "log"
        return _download(
            f"/api/jobs/{job_id}/{artifact}/download",
            arguments["destination_dir"],
            f"{arguments['job_id']}-{artifact}.{suffix}",
        )
    raise ValueError(f"Unknown tool: {name}")


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "kaggle-control-plane", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            value = _call(str(params.get("name", "")), params.get("arguments") or {})
            text = json.dumps(value, ensure_ascii=False, indent=2)
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # MCP tool errors are data, not server crashes.
            return _result(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
    if method and method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = _handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

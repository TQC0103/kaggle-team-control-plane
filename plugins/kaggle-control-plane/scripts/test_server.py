"""Small offline contract tests for the local MCP bridge."""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
from pathlib import Path
from unittest import mock


SERVER_PATH = Path(__file__).with_name("server.py")
SPEC = importlib.util.spec_from_file_location("kcp_server", SERVER_PATH)
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


def test_initialize_and_tools() -> None:
    initialized = SERVER._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized["result"]["serverInfo"]["name"] == "kaggle-control-plane"
    listed = SERVER._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"kcp_status", "kcp_submit_batch", "kcp_download_artifact"} <= names


def test_redaction() -> None:
    value = SERVER._safe({"token": "secret", "nested": {"api_key": "secret", "credential_available": True}})
    assert value == {"token": "[REDACTED]", "nested": {"api_key": "[REDACTED]", "credential_available": True}}


def test_status_summary() -> None:
    responses = [
        ({"accounts": [{"id": "a"}]}, {}),
        ({"states": {"succeeded": 1, "failed": 1}}, {}),
    ]
    with mock.patch.object(SERVER, "_request", side_effect=responses):
        assert SERVER._call("kcp_status", {})["job_states"] == {"succeeded": 1, "failed": 1}


def test_list_jobs_uses_server_side_bounds_and_filters() -> None:
    with mock.patch.object(
        SERVER, "_request", return_value=({"jobs": [{"id": "job_1"}]}, {})
    ) as request:
        result = SERVER._call("kcp_list_jobs", {"status": "submitted", "limit": 7})

    assert result == [{"id": "job_1"}]
    path = request.call_args.args[0]
    assert path.startswith("/api/jobs?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    assert query == {"limit": ["7"], "summary": ["1"], "status": ["submitted"]}


def test_get_job_requests_compact_detail() -> None:
    with mock.patch.object(
        SERVER, "_request", return_value=({"job": {"id": "job_1"}}, {})
    ) as request:
        result = SERVER._call("kcp_get_job", {"job_id": "job_1"})

    assert result == {"job": {"id": "job_1"}}
    path = request.call_args.args[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    assert query == {"include_remote_logs": ["0"], "event_limit": ["100"]}


def test_submit_requires_notebook_folder(tmp_path: Path) -> None:
    job = {
        "account_id": "a",
        "experiment_name": "eval",
        "source_dir": str(tmp_path),
        "kernel_slug": "eval",
        "accelerator": "gpu",
    }
    try:
        SERVER._source_job(job)
    except ValueError as exc:
        assert "kernel-metadata.json" in str(exc)
    else:
        raise AssertionError("missing kernel metadata was accepted")


def test_submit_maps_accelerator_to_backend_metadata(tmp_path: Path) -> None:
    (tmp_path / "kernel-metadata.json").write_text("{}\n", encoding="utf-8")
    cleaned = SERVER._source_job(
        {
            "account_id": "a",
            "experiment_name": "eval",
            "source_dir": str(tmp_path),
            "kernel_slug": "eval",
            "accelerator": "gpu",
            "machine_shape": "NvidiaTeslaT4",
        }
    )

    assert "accelerator" not in cleaned
    assert "machine_shape" not in cleaned
    assert cleaned["metadata"] == {
        "accelerator": "gpu",
        "machine_shape": "NvidiaTeslaT4",
    }


if __name__ == "__main__":
    tests = [
        test_initialize_and_tools,
        test_redaction,
        test_status_summary,
        test_list_jobs_uses_server_side_bounds_and_filters,
        test_get_job_requests_compact_detail,
    ]
    for test in tests:
        test()
    print(json.dumps({"passed": len(tests)}))

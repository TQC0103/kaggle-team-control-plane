"""Verify that the seeded demo dispatched ten jobs to ten distinct accounts."""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the live KCP demo")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--expected-accounts", type=int, default=10)
    parser.add_argument("--expected-jobs", type=int, default=10)
    return parser.parse_args()


def get_json(base_url: str, path: str) -> dict[str, object]:
    request = Request(
        base_url.rstrip("/") + path,
        headers={"Accept": "application/json", "User-Agent": "kcp-demo-verifier/0.1"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"GET {path} returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"could not reach {base_url}: {exc.reason}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"GET {path} did not return a JSON object")
    return value


def require_list(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"response field {key!r} is not an object array")
    return value


def verify(args: argparse.Namespace) -> dict[str, object]:
    base_url = args.url.rstrip("/")
    accounts = require_list(get_json(base_url, "/api/accounts"), "accounts")
    if len(accounts) != args.expected_accounts:
        raise RuntimeError(
            f"expected {args.expected_accounts} demo accounts, found {len(accounts)}"
        )

    batches = require_list(get_json(base_url, "/api/batches"), "batches")
    batch = next(
        (item for item in batches if item.get("name") == "round-1-parallel-demo"),
        None,
    )
    if not batch or not isinstance(batch.get("id"), str):
        raise RuntimeError("round-1-parallel-demo batch was not found")

    deadline = time.monotonic() + max(0.1, args.timeout)
    jobs: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        detail = get_json(base_url, f"/api/batches/{quote(batch['id'], safe='')}")
        raw_batch = detail.get("batch")
        if not isinstance(raw_batch, dict):
            raise RuntimeError("batch detail response is malformed")
        jobs = require_list(raw_batch, "jobs")
        states = {str(job.get("status", "")) for job in jobs}
        if len(jobs) == args.expected_jobs and states <= TERMINAL_STATES:
            break
        time.sleep(0.2)
    else:
        states = {str(job.get("status", "")) for job in jobs}
        raise RuntimeError(
            f"demo did not finish within {args.timeout:g}s; states={sorted(states)}"
        )

    account_ids = {str(job.get("account_id", "")) for job in jobs}
    failures = [
        {"id": job.get("id"), "status": job.get("status"), "error": job.get("error")}
        for job in jobs
        if job.get("status") != "succeeded"
    ]
    if len(jobs) != args.expected_jobs:
        raise RuntimeError(f"expected {args.expected_jobs} jobs, found {len(jobs)}")
    if len(account_ids) != args.expected_accounts:
        raise RuntimeError(
            f"expected {args.expected_accounts} distinct job accounts, found {len(account_ids)}"
        )
    if failures:
        raise RuntimeError("one or more demo jobs failed: " + json.dumps(failures))

    health = get_json(base_url, "/api/health")
    return {
        "ok": True,
        "accounts": len(accounts),
        "jobs": len(jobs),
        "distinct_job_accounts": len(account_ids),
        "status": "succeeded",
        "adapter": health.get("adapter"),
    }


def main() -> int:
    try:
        result = verify(parse_args())
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"demo verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

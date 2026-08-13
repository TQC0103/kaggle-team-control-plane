"""Human/debug CLI using the exact same tool registry as the stdio server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .http_client import ApiClient, ApiError
from .server import JsonRpcServer
from .tools import ToolInputError, ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kaggle-team-agent")
    parser.add_argument("--base-url", help="Control-plane URL (default: env or localhost:8765)")
    parser.add_argument("--api-token", help="Bearer token; prefer KAGGLE_TEAM_API_TOKEN")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Run MCP-compatible JSON-RPC over stdio")
    call = subparsers.add_parser("call", help="Call one tool and print JSON")
    call.add_argument("tool", choices=[
        "list_accounts", "submit_batch", "list_runs", "cancel_run",
        "retry_run", "fetch_result", "audit_events",
    ])
    arguments = call.add_mutually_exclusive_group()
    arguments.add_argument("--arguments", default="{}", help="JSON object (default: {})")
    arguments.add_argument("--file", type=Path, help="Read arguments JSON from a file")
    subparsers.add_parser("tools", help="Print available tool schemas")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    client = ApiClient(base_url=options.base_url, api_token=options.api_token)
    registry = ToolRegistry(client)
    if options.command in (None, "serve"):
        JsonRpcServer(registry).serve()
        return 0
    if options.command == "tools":
        _print_json({"tools": registry.list_tools()})
        return 0
    try:
        raw = options.file.read_text(encoding="utf-8") if options.file else options.arguments
        arguments: Any = json.loads(raw)
        if not isinstance(arguments, dict):
            raise ToolInputError("Arguments must be a JSON object")
        _print_json(registry.call(options.tool, arguments))
        return 0
    except (OSError, json.JSONDecodeError, ToolInputError, ApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())

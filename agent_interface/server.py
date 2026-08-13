"""A dependency-free MCP-compatible JSON-RPC stdio server."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .http_client import ApiError
from .tools import ToolInputError, ToolRegistry


PROTOCOL_VERSION = "2024-11-05"


class JsonRpcServer:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()

    def dispatch(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return _error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(request_id, -32600, "Invalid Request")

        # JSON-RPC notifications deliberately receive no response.
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            return _result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "kaggle-team-agent", "version": "0.1.0"},
                },
            )
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": self.registry.list_tools()})
        if method == "tools/call":
            return self._call_tool(request_id, request.get("params"))
        return _error(request_id, -32601, f"Method not found: {method}")

    def _call_tool(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "tools/call requires a tool name")
        try:
            payload = self.registry.call(params["name"], params.get("arguments"))
            return _result(request_id, _tool_result(payload, is_error=False))
        except (ToolInputError, ApiError) as exc:
            details = exc.as_dict() if isinstance(exc, ApiError) else {"error": str(exc)}
            return _result(request_id, _tool_result(details, is_error=True))
        except Exception as exc:  # Keep stdio alive; never emit tracebacks on stdout.
            print(f"Unexpected tool failure: {type(exc).__name__}: {exc}", file=sys.stderr)
            return _result(
                request_id,
                _tool_result({"error": "Internal agent-interface error"}, is_error=True),
            )

    def serve(self, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout) -> None:
        for raw_line in input_stream:
            if not raw_line.strip():
                continue
            try:
                request = json.loads(raw_line)
            except json.JSONDecodeError:
                response = _error(None, -32700, "Parse error")
            else:
                response = self.dispatch(request)
            if response is not None:
                output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
                output_stream.flush()


def _result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_result(payload: Any, *, is_error: bool) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


def main() -> int:
    JsonRpcServer().serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

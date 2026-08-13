"""Tool definitions and HTTP mappings exposed to coding agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from .http_client import ApiClient


ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolInputError(ValueError):
    pass


class ToolRegistry:
    def __init__(self, client: ApiClient | None = None) -> None:
        self.client = client or ApiClient()
        self._tools = self._build_tools()

    def list_tools(self) -> list[dict[str, Any]]:
        return [tool.descriptor() for tool in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolInputError(f"Unknown tool: {name}")
        args = arguments or {}
        if not isinstance(args, dict):
            raise ToolInputError("Tool arguments must be a JSON object")
        return tool.handler(args)

    def _build_tools(self) -> dict[str, Tool]:
        tools = [
            Tool(
                "list_accounts",
                "List team-owned Kaggle accounts, quota state, and remote-reconciliation blocks. Never returns credentials.",
                _object_schema({}),
                self._list_accounts,
            ),
            Tool(
                "submit_batch",
                "Submit experiments concurrently. Every experiment must name its explicit account_id.",
                _object_schema(
                    {
                        "name": {"type": "string", "description": "Human-readable batch name."},
                        "experiments": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": {
                                "type": "object",
                                "required": [
                                    "account_id",
                                    "experiment_name",
                                    "source_dir",
                                    "kernel_slug",
                                ],
                                "properties": {
                                    "account_id": {"type": "string", "minLength": 1},
                                    "experiment_name": {"type": "string"},
                                    "source_dir": {"type": "string"},
                                    "kernel_slug": {"type": "string"},
                                    "metadata": {"type": "object"},
                                },
                                "additionalProperties": False,
                            },
                        },
                    },
                    required=["name", "experiments"],
                ),
                self._submit_batch,
            ),
            Tool(
                "list_runs",
                "List experiment runs, optionally filtered by batch, account, or status.",
                _object_schema(
                    {
                        "batch_id": {"type": "string"},
                        "account_id": {"type": "string"},
                        "status": {"type": "string"},
                    }
                ),
                self._list_runs,
            ),
            Tool(
                "cancel_run",
                (
                    "Cancel a queued experiment or stop local monitoring of an active one. "
                    "Kaggle may keep an active remote kernel running; in that case the account "
                    "is blocked until a human verifies the remote run and reconciles it in the dashboard."
                ),
                _object_schema(
                    {"run_id": {"type": "string", "minLength": 1}},
                    required=["run_id"],
                ),
                self._cancel_run,
            ),
            Tool(
                "retry_run",
                (
                    "Retry a failed or cancelled experiment on its original, explicit account. "
                    "The control plane refuses the retry while that account needs remote "
                    "reconciliation or has exhausted its configured quota."
                ),
                _object_schema(
                    {"run_id": {"type": "string", "minLength": 1}},
                    required=["run_id"],
                ),
                self._retry_run,
            ),
            Tool(
                "fetch_result",
                "Fetch the result manifest, metrics, logs, and artifact references for a run.",
                _object_schema(
                    {"run_id": {"type": "string", "minLength": 1}}, required=["run_id"]
                ),
                self._fetch_result,
            ),
            Tool(
                "audit_events",
                "Read immutable control-plane audit events with optional filters.",
                _object_schema(
                    {
                        "entity_type": {"type": "string"},
                        "entity_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    }
                ),
                self._audit_events,
            ),
        ]
        return {tool.name: tool for tool in tools}

    def _list_accounts(self, args: dict[str, Any]) -> Any:
        return self.client.request("GET", "/api/accounts")

    def _submit_batch(self, args: dict[str, Any]) -> Any:
        name = _required_string(args, "name")
        experiments = _required_list(args, "experiments")
        if len(experiments) > 10:
            raise ToolInputError("experiments must contain at most 10 items in MVP round 1")
        for index, experiment in enumerate(experiments):
            if not isinstance(experiment, dict):
                raise ToolInputError(f"experiments[{index}] must be an object")
            for key in ("account_id", "experiment_name", "source_dir", "kernel_slug"):
                _required_string(experiment, key, prefix=f"experiments[{index}].")
        jobs = [
            _pick(
                experiment,
                "account_id",
                "experiment_name",
                "source_dir",
                "kernel_slug",
                "metadata",
            )
            for experiment in experiments
        ]
        body = {"name": name, "jobs": jobs}
        return self.client.request("POST", "/api/batches", body=body)

    def _list_runs(self, args: dict[str, Any]) -> Any:
        return self.client.request(
            "GET",
            "/api/jobs",
            query=_pick(args, "batch_id", "account_id", "status"),
        )

    def _cancel_run(self, args: dict[str, Any]) -> Any:
        run_id = _required_string(args, "run_id")
        return self.client.request(
            "POST",
            f"/api/jobs/{_path_id(run_id)}/cancel",
            body={},
        )

    def _retry_run(self, args: dict[str, Any]) -> Any:
        run_id = _required_string(args, "run_id")
        return self.client.request(
            "POST",
            f"/api/jobs/{_path_id(run_id)}/retry",
            body={},
        )

    def _fetch_result(self, args: dict[str, Any]) -> Any:
        run_id = _required_string(args, "run_id")
        return self.client.request("GET", f"/api/jobs/{_path_id(run_id)}/result")

    def _audit_events(self, args: dict[str, Any]) -> Any:
        return self.client.request(
            "GET",
            "/api/audit",
            query=_pick(args, "entity_type", "entity_id", "limit"),
        )


def _object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _pick(values: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: values[key] for key in keys if key in values and values[key] is not None}


def _required_list(values: dict[str, Any], key: str) -> list[Any]:
    value = values.get(key)
    if not isinstance(value, list) or not value:
        raise ToolInputError(f"{key} must be a non-empty array")
    return value


def _required_string(values: dict[str, Any], key: str, prefix: str = "") -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{prefix}{key} must be a non-empty string")
    return value


def _path_id(value: str) -> str:
    return quote(value, safe="")

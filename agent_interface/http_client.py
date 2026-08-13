"""Small dependency-free HTTP client for the local control-plane API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class ApiError(RuntimeError):
    """A safe-to-report control-plane API error."""

    message: str
    status: int | None = None
    details: Any = None

    def __str__(self) -> str:
        prefix = f"HTTP {self.status}: " if self.status is not None else ""
        return prefix + self.message

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"error": self.message}
        if self.status is not None:
            result["status"] = self.status
        if self.details is not None:
            result["details"] = self.details
        return result


class ApiClient:
    """Call the Kaggle Team control plane without storing account credentials."""

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        configured_url = base_url or os.getenv("KAGGLE_TEAM_API_URL") or DEFAULT_BASE_URL
        self.base_url = configured_url.rstrip("/")
        self.api_token = (
            api_token if api_token is not None else os.getenv("KAGGLE_TEAM_API_TOKEN")
        )
        configured_timeout = os.getenv("KAGGLE_TEAM_API_TIMEOUT_SECONDS")
        self.timeout = (
            timeout
            if timeout is not None
            else float(configured_timeout or DEFAULT_TIMEOUT_SECONDS)
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        query_values = {
            key: _query_value(value)
            for key, value in (query or {}).items()
            if value is not None
        }
        url = self.base_url + "/" + path.lstrip("/")
        if query_values:
            url += "?" + urlencode(query_values)

        headers = {"Accept": "application/json", "User-Agent": "kaggle-team-agent/0.1"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")

        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ApiError("Control-plane response exceeded 8 MiB")
                if not raw:
                    return {"ok": True, "status": response.status}
                return _decode_json(raw, response.status)
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES)
            details = _try_decode_json(raw)
            message = _error_message(details) or exc.reason or "Control-plane request failed"
            raise ApiError(str(message), status=exc.code, details=details) from None
        except URLError as exc:
            raise ApiError(f"Could not reach control plane at {self.base_url}: {exc.reason}") from None
        except TimeoutError:
            raise ApiError(f"Control-plane request timed out after {self.timeout:g}s") from None


def _query_value(value: Any) -> str | int | float:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _decode_json(raw: bytes, status: int) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("Control plane returned invalid JSON", status=status) from exc


def _try_decode_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8", errors="replace")
        return {"response": text[:2000]}


def _error_message(details: Any) -> str | None:
    if isinstance(details, dict):
        for key in ("error", "message", "detail"):
            value = details.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = value.get("message")
                if isinstance(nested, str) and nested:
                    return nested
    return None

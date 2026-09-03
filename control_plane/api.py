"""Dependency-free HTTP/JSON API for the Kaggle control plane."""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import shutil
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import ControlPlaneError, NotFoundError, ValidationError
from .service import ControlPlaneService


LOCAL_ORIGIN = re.compile(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$")
MAX_BODY_BYTES = 2 * 1024 * 1024


class ControlPlaneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: ControlPlaneService,
        api_token: str | None = None,
    ):
        self.service = service
        self.api_token = api_token
        super().__init__(server_address, ControlPlaneRequestHandler)


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server: ControlPlaneHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the default useful request log while avoiding request bodies.
        # Windowed desktop executables intentionally have no stderr stream.
        if sys.stderr is not None:
            super().log_message(format, *args)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Authorization, Content-Type"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path not in {"/health", "/api/health"}:
                self._authorize_origin()
                self._authorize()
            query = parse_qs(parsed.query)
            # Audit identity comes from server-observed authentication mode.
            # X-Actor is deliberately ignored because callers can forge it.
            actor = (
                "authenticated-client" if self.server.api_token else "local-client"
            )
            if method == "GET" and self._download(path):
                return
            body = self._read_json() if method in {"POST", "PATCH"} else None
            status, payload = self._route(method, path, query, body, actor)
            self._send_json(status, payload)
        except ControlPlaneError as exc:
            self._send_json(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        except json.JSONDecodeError:
            self._send_json(
                400, {"error": {"code": "invalid_json", "message": "invalid JSON body"}}
            )
        except Exception as exc:
            self._send_json(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "internal server error",
                    }
                },
            )
            self.log_error("unhandled request error: %s", exc)

    def _download(self, path: str) -> bool:
        if path == "/api/support-bundle/download":
            filename, file_path = self.server.service.support_bundle_download()
            self._send_download_headers(
                filename, "application/zip", file_path.stat().st_size
            )
            with file_path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
            return True
        logs = re.fullmatch(r"/api/jobs/([^/]+)/logs/download", path)
        if logs:
            filename, file_path = self.server.service.job_logs_download(logs.group(1))
            self._send_download_headers(
                filename, "text/plain; charset=utf-8", file_path.stat().st_size
            )
            with file_path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
            return True
        result = re.fullmatch(r"/api/jobs/([^/]+)/result/download", path)
        if result:
            filename, file_path = self.server.service.job_result_download(result.group(1))
            self._send_download_headers(filename, "application/zip", file_path.stat().st_size)
            with file_path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
            return True
        return False

    def _send_download_headers(
        self, filename: str, content_type: str, content_length: int
    ) -> None:
        safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _authorize(self) -> None:
        expected = self.server.api_token
        if not expected:
            return
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied, expected):
            raise ControlPlaneError("unauthorized", status=401, code="unauthorized")

    def _authorize_origin(self) -> None:
        """Allow local dashboard origins and non-browser CLI/agent requests only."""
        origin = self.headers.get("Origin")
        if origin and not LOCAL_ORIGIN.fullmatch(origin):
            raise ControlPlaneError(
                "browser origin is not allowed", status=403, code="origin_forbidden"
            )

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0:
            raise ValidationError("Content-Length must not be negative")
        if length > MAX_BODY_BYTES:
            raise ControlPlaneError(
                "request body is too large", status=413, code="payload_too_large"
            )
        raw = self.rfile.read(length)
        if not raw:
            return {}
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ControlPlaneError(
                "Content-Type must be application/json",
                status=415,
                code="unsupported_media_type",
            )
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValidationError("JSON body must be an object")
        return value

    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any] | None,
        actor: str,
    ) -> tuple[int, dict[str, Any]]:
        service = self.server.service
        body = body or {}
        if method == "GET" and path in {"/health", "/api/health"}:
            return 200, service.health()
        if method == "GET" and path == "/api/accounts":
            return 200, {"accounts": service.list_accounts()}
        if method == "POST" and path == "/api/credentials/inspect":
            return 200, service.inspect_credential(body)
        if method == "GET" and path == "/api/credentials":
            return 200, {"credentials": service.list_credential_refs()}
        if method == "GET" and path == "/api/sources":
            return 200, service.browse_sources(self._query_one(query, "path"))
        if method == "POST" and path == "/api/accounts":
            return 201, {"account": service.create_account(body, actor)}
        match = re.fullmatch(r"/api/accounts/([^/]+)", path)
        if match and method == "GET":
            return 200, {"account": service.get_account(match.group(1))}
        if match and method == "PATCH":
            return 200, {"account": service.update_account(match.group(1), body, actor)}
        match = re.fullmatch(r"/api/accounts/([^/]+)/revoke", path)
        if match and method == "POST":
            return 200, {"account": service.revoke_account(match.group(1), actor)}
        match = re.fullmatch(r"/api/accounts/([^/]+)/reconcile", path)
        if match and method == "POST":
            return 200, service.reconcile_account(match.group(1), body, actor)
        match = re.fullmatch(r"/api/accounts/([^/]+)/quota/sync", path)
        if match and method == "POST":
            return 200, service.sync_account_quota(match.group(1))

        if method == "GET" and path == "/api/batches":
            return 200, {"batches": service.list_batches()}
        if method == "POST" and path == "/api/batches":
            return 202, {"batch": service.create_batch(body, actor)}
        match = re.fullmatch(r"/api/batches/([^/]+)", path)
        if match and method == "GET":
            return 200, {"batch": service.get_batch(match.group(1))}

        if method == "GET" and path == "/api/jobs":
            raw_limit = self._query_one(query, "limit")
            try:
                limit = int(raw_limit) if raw_limit else None
            except ValueError as exc:
                raise ValidationError("job limit must be an integer") from exc
            if limit is not None and not 1 <= limit <= 1000:
                raise ValidationError("job limit must be between 1 and 1000")
            return 200, {
                "jobs": service.list_jobs(
                    batch_id=self._query_one(query, "batch_id"),
                    account_id=self._query_one(query, "account_id"),
                    status=self._query_one(query, "status"),
                    limit=limit,
                    summary=self._query_one(query, "summary") in {"1", "true"},
                )
            }
        if method == "GET" and path == "/api/jobs/stats":
            return 200, {"states": service.job_state_counts()}
        match = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if match and method == "GET":
            raw_event_limit = self._query_one(query, "event_limit") or "200"
            raw_log_limit = self._query_one(query, "remote_log_limit") or "500"
            try:
                event_limit = int(raw_event_limit)
                remote_log_limit = int(raw_log_limit)
            except ValueError as exc:
                raise ValidationError("job detail limits must be integers") from exc
            if not 1 <= event_limit <= 1000 or not 1 <= remote_log_limit <= 500:
                raise ValidationError("job detail limits are out of range")
            return 200, {
                "job": service.get_job(
                    match.group(1),
                    include_remote_logs=self._query_one(query, "include_remote_logs")
                    not in {"0", "false"},
                    event_limit=event_limit,
                    remote_log_limit=remote_log_limit,
                )
            }
        match = re.fullmatch(r"/api/jobs/([^/]+)/remote-status", path)
        if match and method == "GET":
            return 200, service.remote_job_status(match.group(1))
        match = re.fullmatch(r"/api/jobs/([^/]+)/cancel", path)
        if match and method == "POST":
            return 202, service.cancel_job(match.group(1), actor)
        match = re.fullmatch(r"/api/jobs/([^/]+)/retry", path)
        if match and method == "POST":
            return 202, {"job": service.retry_job(match.group(1), actor)}
        match = re.fullmatch(r"/api/jobs/([^/]+)/result", path)
        if match and method == "GET":
            return 200, service.job_result(match.group(1))
        match = re.fullmatch(r"/api/jobs/([^/]+)/events", path)
        if match and method == "GET":
            raw_limit = self._query_one(query, "limit") or "200"
            raw_before = self._query_one(query, "before_id")
            try:
                limit = int(raw_limit)
                before_id = int(raw_before) if raw_before else None
            except ValueError as exc:
                raise ValidationError("event pagination values must be integers") from exc
            return 200, service.job_events_page(
                match.group(1), before_id=before_id, limit=limit
            )
        match = re.fullmatch(r"/api/jobs/([^/]+)/logs", path)
        if match and method == "GET":
            raw_limit = self._query_one(query, "limit") or "200"
            raw_before = self._query_one(query, "before_id")
            try:
                limit = int(raw_limit)
                before_id = int(raw_before) if raw_before else None
            except ValueError as exc:
                raise ValidationError("log pagination values must be integers") from exc
            return 200, service.job_remote_logs_page(
                match.group(1), before_id=before_id, limit=limit
            )

        if method == "GET" and path == "/api/audit":
            raw_limit = self._query_one(query, "limit") or "100"
            try:
                limit = int(raw_limit)
            except ValueError as exc:
                raise ValidationError("limit must be an integer") from exc
            return 200, {
                "audit": service.list_audit(
                    limit=limit,
                    entity_type=self._query_one(query, "entity_type"),
                    entity_id=self._query_one(query, "entity_id"),
                )
            }
        raise NotFoundError(f"route {method} {path} was not found")

    @staticmethod
    def _query_one(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if LOCAL_ORIGIN.fullmatch(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def create_server(
    service: ControlPlaneService,
    host: str = "127.0.0.1",
    port: int = 8765,
    api_token: str | None = None,
) -> ControlPlaneHTTPServer:
    normalized_host = host.strip().lower()
    try:
        loopback = ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        loopback = normalized_host == "localhost"
    if not loopback and not api_token:
        raise ValidationError("KCP_API_TOKEN is required when binding beyond loopback")
    return ControlPlaneHTTPServer((host, port), service, api_token)

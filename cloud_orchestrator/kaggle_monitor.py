"""Read-only exact-version Kaggle observations using private cloud vaults.

No submit/cancel/push API, desktop credential access or plaintext auth files.
Fresh access tokens stay in memory; refresh tokens are not rewritten.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import logging
import re
import time

from .connection import PROJECT, PROJECT_NUMBER, ACCOUNT, USERNAME
from .protocol import SHA, SLUG


class MonitorMismatch(ValueError):
    """Only fixed, non-sensitive local reason codes may be returned."""


def bounded_client(**kwargs):
    from kagglesdk import KaggleEnv
    from kagglesdk.kaggle_client import KaggleClient
    logging.getLogger("kagglesdk").setLevel(logging.CRITICAL)
    # An explicit empty token blocks SDK fallback to unrelated environment or
    # ~/.kaggle files; refresh clients are deliberately anonymous.
    kwargs.setdefault("api_token", "")
    client = KaggleClient(env=KaggleEnv.PROD, verbose=False, **kwargs)
    # SDK0.1.38 exposes no timeout parameter; bound only this client's session.
    # No global monkeypatch and no requests/response debug output.
    http = client.http_client()
    http._init_session()
    if not kwargs["api_token"]:
        http._session.auth = ((kwargs["username"], kwargs["password"])
                              if kwargs.get("username") and kwargs.get("password") else None)
    http._session.send = partial(http._session.send, timeout=(2, 4))
    return client


def validate_monitor(row):
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(row.get("id", "")))
            or not ACCOUNT.fullmatch(str(row.get("account_id", "")))
            or not SLUG.fullmatch(str(row.get("kernel_slug", "")))
            or type(row.get("version")) is not int or row["version"] < 1
            or not SHA.fullmatch(str(row.get("source_sha256", "")))):
        raise ValueError("invalid exact monitor identity")


class GoogleKaggleSessions:
    def __init__(self, configurations, secret_client=None):
        if not 1 <= len(configurations) <= 2:
            raise ValueError("at most two private vaults")
        self.configurations = configurations
        for c in configurations:
            match = re.fullmatch(r"projects/([^/]+)/secrets/(kcp-kaggle-vault-[a-f0-9]{12}-g0[12])/versions/([1-9][0-9]*)", str(c.get("version_name", "")))
            if not match or match[1] not in {PROJECT, PROJECT_NUMBER} or not SHA.fullmatch(str(c.get("plan_sha256", ""))):
                raise ValueError("vault identity is outside the selected project")
        if secret_client is None:
            from google.cloud import secretmanager
            secret_client = secretmanager.SecretManagerServiceClient()
        self.client = secret_client

    def load(self):
        accounts = {}
        for c in self.configurations:
            response = self.client.access_secret_version(request={"name": c["version_name"]}, retry=None, timeout=12)
            raw = bytes(response.payload.data)
            if len(raw) > 60000:
                raise ValueError("bounded vault size exceeded")
            bundle = json.loads(raw)
            if bundle.get("schema") != 1 or bundle.get("plan_sha256") != c["plan_sha256"] or not 1 <= len(bundle.get("accounts", [])) <= 4:
                raise ValueError("vault plan mismatch")
            for item in bundle["accounts"]:
                identity, username = item.get("id"), item.get("username")
                if (not ACCOUNT.fullmatch(str(identity)) or not USERNAME.fullmatch(str(username))
                        or identity in accounts or item.get("kind") not in {"kaggle_oauth_v1", "kaggle_static_v1"}):
                    raise ValueError("vault account identity mismatch")
                accounts[identity] = item
        return accounts

    @contextmanager
    def authenticated(self, item):
        username, auth = item["username"], item["auth"]
        if item["kind"] == "kaggle_oauth_v1":
            from kagglesdk import KaggleCredentials
            if auth.get("username") != username or not auth.get("refresh_token"):
                raise ValueError("OAuth identity mismatch")
            class InMemoryCredentials(KaggleCredentials):
                def save(self, file_path=None):
                    pass
            with bounded_client() as anonymous:
                credentials = InMemoryCredentials(client=anonymous, refresh_token=auth["refresh_token"],
                    username=username, scopes=auth.get("scopes") or ["resources.admin:*"])
                token = credentials.get_access_token()
                if not token or credentials.introspect() != username:
                    raise ValueError("cloud refresh identity mismatch")
            with bounded_client(api_token=token) as client:
                yield client
        else:
            aliases = {"token": "api_token", "KAGGLE_API_TOKEN": "api_token", "key": "password", "KAGGLE_KEY": "password"}
            opts = {aliases[k]: v for k, v in auth.items() if k in aliases and v}
            if not opts:
                raise ValueError("missing static auth")
            opts["username"] = username
            with bounded_client(**opts) as client:
                yield client


def probe(client, item, row, clock=time.time):
    """Fence the current-version API with immutable source/version before/after.

    Live V1 API rejected version_label (404) and slug/version (403). Never label
    an unguarded latest response as exact-version evidence.
    """
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest, ApiGetKernelSessionStatusRequest
    validate_monitor(row)
    owner, slug = row["kernel_slug"].split("/")
    if row["account_id"] != item["id"] or owner != item["username"]:
        raise MonitorMismatch("ACCOUNT_BINDING_MISMATCH")
    req = ApiGetKernelRequest()
    req.user_name, req.kernel_slug = owner, slug
    kernel = client.kernels.kernels_api_client.get_kernel(req)
    meta = kernel.metadata
    if not meta or not kernel.blob or not meta.is_private or meta.current_version_number != row["version"]:
        raise MonitorMismatch("PRIVATE_VERSION_MISMATCH")
    # author is a human display name, not an account ID (observed on TQC).
    if meta.ref.casefold() != row['kernel_slug'].casefold() or meta.slug != slug:
        raise MonitorMismatch("KERNEL_IDENTITY_MISMATCH")
    source = hashlib.sha256(kernel.blob.source.encode("utf-8")).hexdigest()
    if source != row["source_sha256"]:
        raise MonitorMismatch("SOURCE_SHA_MISMATCH")
    status_request = ApiGetKernelSessionStatusRequest()
    status_request.user_name, status_request.kernel_slug = owner, slug
    result = client.kernels.kernels_api_client.get_kernel_session_status(status_request)
    after = client.kernels.kernels_api_client.get_kernel(req)
    if (not after.metadata or not after.blob or not after.metadata.is_private
            or after.metadata.current_version_number != row["version"]
            or after.metadata.id != meta.id or after.metadata.ref.casefold() != row['kernel_slug'].casefold()
            or after.metadata.slug != slug
            or hashlib.sha256(after.blob.source.encode("utf-8")).hexdigest() != source):
        raise MonitorMismatch("SOURCE_OR_VERSION_CHANGED_DURING_STATUS")
    if row.get("kernel_id") is not None and meta.id != row["kernel_id"]:
        raise MonitorMismatch("KERNEL_ID_CHANGED")
    name = getattr(result.status, "name", "UNKNOWN").lower()
    state = name if name in {"queued", "running", "complete", "error", "cancelled"} else "unknown"
    if state == "error":
        state = "failed"
    return dict(id=row["id"], account_id=row["account_id"], kernel_slug=row["kernel_slug"],
                version=row["version"], ref=f"{row['kernel_slug']}/{row['version']}",
                status=state, source_sha256=source, observed_at=clock(), is_private=True,
                machine_shape=meta.machine_shape, kernel_id=meta.id,
                observation_backend="official-kagglesdk-source-fenced-current-version")


class KaggleObserver:
    def __init__(self, sessions):
        self.sessions = sessions

    def observe(self, monitors, deadline, clock=time.time):
        if len(monitors) > 4:
            raise ValueError("at most four monitors per finite tick")
        accounts = self.sessions.load()
        results = {}
        for row in monitors:
            if clock() >= deadline:
                break
            try:
                validate_monitor(row)
                item = accounts[row["account_id"]]
                with self.sessions.authenticated(item) as client:
                    results[row["id"]] = probe(client, item, row, clock)
            except Exception as exc:
                # Exception text/API response could contain auth. Class only.
                identity = row.get("id")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(identity)):
                    identity = "invalid"
                results[identity] = dict(status="OBSERVATION_UNAVAILABLE", error_class=type(exc).__name__,
                                         observed_at=clock(), submission_enabled=False)
                if isinstance(exc, MonitorMismatch):
                    results[identity]["reason"] = str(exc)
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if type(status) is int and status in {400, 401, 403, 404, 429, 500, 502, 503, 504}:
                    results[identity]["http_status"] = status
                url = str(getattr(getattr(exc, "request", None), "url", ""))
                for route, phase in [("GenerateAccessToken", "oauth_refresh"),
                                     ("IntrospectToken", "oauth_introspection"),
                                     ("GetKernelSessionStatus", "kernel_status"),
                                     ("GetKernel", "kernel_source")]:
                    if route in url:
                        results[identity]["phase"] = phase
                        break
        return results

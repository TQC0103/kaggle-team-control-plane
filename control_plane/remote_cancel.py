"""Cancel an explicitly identified Kaggle session, without guessing IDs.

The public status/GetKernel APIs do not resolve owner/slug to a session ID.
Notebook IDs and per-notebook version numbers are deliberately never used.
Credentials remain in the assigned account's in-memory subprocess environment.
"""
from __future__ import annotations

import functools
import re
from typing import Any
from urllib.parse import urlsplit

from .errors import ValidationError


def submitted_version(job: dict[str, Any]) -> int:
    submit = (job.get("result") or {}).get("submit") or {}
    version = submit.get("version_number")
    if version is None:
        match = re.search(r"\bKernel version (\d+) successfully pushed\b", str(submit.get("cli_message") or ""), re.IGNORECASE)
        version = int(match.group(1)) if match else None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("The submitted notebook version is unavailable; cannot safely resolve its session")
    return version


def parse_session_redirect(location: str) -> int:
    url = urlsplit(location)
    match = re.fullmatch(r"/(?:api/)?v1/kernels/output/download_zip/([1-9]\d*)/?", url.path)
    if url.scheme != "https" or url.hostname != "api.kaggle.com" or url.username or url.password or url.port not in (None,443) or not match:
        raise ValidationError("Kaggle did not return a recognised session-specific output redirect")
    return int(match.group(1))


def _client(env, owner):
    from kagglesdk.kaggle_client import KaggleClient
    token, key = env.get("KAGGLE_API_TOKEN") or None, env.get("KAGGLE_KEY") or None
    if not token and not key:
        raise ValidationError("Assigned account authentication is unavailable")
    return KaggleClient(api_token=token, username=owner if key else None, password=key)


def _owner(job, env):
    ref = job["kernel_slug"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", ref):
        raise ValidationError("Invalid job notebook reference")
    owner = ref.split("/",1)[0]
    if owner.casefold() != env.get("KAGGLE_USERNAME", "").casefold():
        raise ValidationError("Cancellation must use the job's assigned Kaggle owner")
    return owner


def resolve_session_identity(job: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Resolve owner/slug/version using Kaggle's read-only output redirect.

    Stop at the first redirect: no ZIP download, signed URL, internal web API,
    or speculative ID. Pin to the version from the original push receipt.
    """
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest, ApiDownloadKernelOutputRequest
    owner = _owner(job, env)
    slug = job["kernel_slug"].split("/",1)[1]
    version = submitted_version(job)
    with _client(env, owner) as client:
        http = client.http_client()
        http._session.send = functools.partial(http._session.send, timeout=20)
        request = ApiGetKernelRequest(); request.user_name=owner; request.kernel_slug=slug
        metadata = client.kernels.kernels_api_client.get_kernel(request).metadata
        if metadata.ref != job["kernel_slug"] or metadata.current_version_number != version:
            raise ValidationError("Kaggle notebook/version did not match the original submission")
        request = ApiDownloadKernelOutputRequest()
        request.owner_slug=owner; request.kernel_slug=slug; request.version_number=version
        prepared = http._prepare_request("kernels.KernelsApiService", "DownloadKernelOutput", request)
        # Do not use SDK _prepare_response: it attempts JSON decoding on some
        # empty 302 responses. Read only the status and Location, then close.
        response = http._session.send(prepared, allow_redirects=False, stream=True, timeout=20)
        try:
            response.raise_for_status()
            if response.status_code not in (301,302,303,307,308):
                raise ValidationError("Kaggle output API did not return a session redirect")
            session_id = parse_session_redirect(response.headers.get("Location", ""))
        finally:
            response.close()
    return {"kernel_session_id":session_id, "session_ref":job["kernel_slug"],
            "version_number":version, "notebook_id":metadata.id,
            "method":"official_output_download_redirect", "remote_mutation":False}


def cancellation_identity(job: dict[str, Any], payload: dict[str, Any]) -> int | None:
    if not isinstance(payload, dict):
        raise ValidationError("Cancellation body must be an object")
    unknown = set(payload) - {"kernel_session_id", "session_ref"}
    if unknown:
        raise ValidationError("Unsupported cancellation fields")
    metadata = job.get("metadata") or {}
    identity = payload if payload else metadata
    session_id = identity.get("kernel_session_id")
    if session_id is None:
        return None
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
        raise ValidationError("kernel_session_id must be a positive integer")
    if identity.get("session_ref") != job["kernel_slug"]:
        raise ValidationError("session_ref must match the job's exact owner/kernel-slug")
    return session_id


def cancel_session(job: dict[str, Any], env: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    session_id = cancellation_identity(job, payload)
    if session_id is None:
        return {"request_sent": False, "semantics": "remote_cancellation_requires_session_id",
                "message": "Kaggle requires the actual kernel session ID. The public CLI cannot resolve it from a notebook slug; the remote session has NOT been cancelled."}
    owner = _owner(job, env)
    # Import the small generated SDK, never KaggleApi (which drags scientific
    # packages into the desktop build) and never a credentials file.
    from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest
    with _client(env, owner) as client:
        http = client.http_client()
        http._session.send = functools.partial(http._session.send, timeout=20)
        request = ApiCancelKernelSessionRequest()
        request.kernel_session_id = session_id
        # Exactly one mutation. Do not automatically replay on auth/network
        # failure: the server may already have accepted it.
        response = client.kernels.kernels_api_client.cancel_kernel_session(request)
        if response.error_message:
            raise ValidationError("Kaggle rejected session cancellation: " + response.error_message)
    return {"request_sent": True, "kernel_session_id": session_id,
            "session_ref": job["kernel_slug"], "semantics": "remote_cancellation_pending",
            "message": "Kaggle accepted the cancellation request; waiting for remote terminal status."}

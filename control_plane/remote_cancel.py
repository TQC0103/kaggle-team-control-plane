"""Cancel an explicitly identified Kaggle session, without guessing IDs.

The public status/GetKernel APIs do not resolve owner/slug to a session ID.
Notebook IDs and per-notebook version numbers are deliberately never used.
Credentials remain in the assigned account's in-memory subprocess environment.
"""
from __future__ import annotations

import functools
import re
from typing import Any

from .errors import ValidationError


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
    owner, _ = job["kernel_slug"].split("/", 1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", job["kernel_slug"]) or owner.casefold() != env.get("KAGGLE_USERNAME", "").casefold():
        raise ValidationError("Cancellation must use the job's assigned Kaggle owner")
    # Import the small generated SDK, never KaggleApi (which drags scientific
    # packages into the desktop build) and never a credentials file.
    from kagglesdk.kaggle_client import KaggleClient
    from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest

    token = env.get("KAGGLE_API_TOKEN")
    key = env.get("KAGGLE_KEY")
    if not token and not key:
        raise ValidationError("Assigned account authentication is unavailable")
    with KaggleClient(api_token=token, username=owner if key else None, password=key) as client:
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

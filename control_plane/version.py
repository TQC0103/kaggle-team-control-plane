"""Build identity shared by the API, desktop shell, and release artifacts."""

from __future__ import annotations

import os


DEFAULT_APP_VERSION = "0.2.0-beta.1"

try:
    from ._build import APP_VERSION as _PACKAGED_APP_VERSION
    from ._build import BUILD_SHA as _PACKAGED_BUILD_SHA
except ImportError:  # Generated only while packaging the desktop app.
    _PACKAGED_APP_VERSION = DEFAULT_APP_VERSION
    _PACKAGED_BUILD_SHA = "development"

APP_VERSION = os.environ.get("KCP_VERSION", _PACKAGED_APP_VERSION).strip() or DEFAULT_APP_VERSION
BUILD_SHA = os.environ.get("KCP_BUILD_SHA", _PACKAGED_BUILD_SHA).strip() or "development"


def build_identity() -> dict[str, str]:
    return {"version": APP_VERSION, "build_sha": BUILD_SHA}

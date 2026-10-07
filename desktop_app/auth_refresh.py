"""Refresh desktop OAuth in memory and DPAPI, with per-account serialization."""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from control_plane.errors import ValidationError
from .oauth import parse_oauth_bundle, resolve_oauth_access_token, serialize_oauth_bundle


class DesktopAuthRefresh:
    def __init__(self, store, *, environ=None, resolver=None, clock=None):
        self.store = store
        self.environ = os.environ if environ is None else environ
        self.resolver = resolver or resolve_oauth_access_token
        self.clock = clock or time.monotonic
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._next_check: dict[str, float] = {}
        self._states: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _lock(self, ref: str):
        with self._guard:
            return self._locks.setdefault(ref, threading.RLock())

    def invalidate(self, ref: str) -> None:
        with self._lock(ref):
            self._next_check.pop(ref, None)
            self._states.pop(ref, None)

    def status(self, ref: str | None) -> dict[str, str]:
        if not ref:
            return {"state": "missing"}
        # Account/UI reads must not wait for a network refresh holding its lock.
        return {"state": self._states.get(ref, "unchecked")}

    def save(self, ref: str, stored: str) -> None:
        with self._lock(ref):
            self.store.save(ref, stored)
            bundle = parse_oauth_bundle(stored)
            self.environ[ref] = str(bundle.get("access_token") or "") if bundle else stored
            self.invalidate(ref)

    def forget(self, ref: str) -> bool:
        with self._lock(ref):
            removed = self.store.forget(ref)
            self.environ.pop(ref, None)
            self.invalidate(ref)
            return removed

    def ensure(self, ref: str, *, force=False, observed_token=None) -> bool:
        with self._lock(ref):
            now = self.clock()
            # Another request already refreshed this worker's old token.
            if force and observed_token is not None and self.environ.get(ref) != observed_token:
                force = False
            if now < self._next_check.get(ref, 0) and (not force or self._states.get(ref) in {"temporarily_unavailable", "relogin_required"}):
                if self._states.get(ref) in {"temporarily_unavailable", "relogin_required"}:
                    raise ValidationError(self._safe_error(ref))
                return self._states.get(ref) == "ready"
            self._states[ref] = "refreshing"
            try:
                # Auth material is handled only by the desktop boundary.
                stored = self.store.load(ref)
                bundle = parse_oauth_bundle(stored)
                if bundle is None:
                    self.environ[ref] = stored
                    self._states[ref] = "static_credential"
                    self._next_check[ref] = now + 60
                    return False
                token, renewed = self.resolver(bundle, force=force)
                if not token:
                    raise ValueError("empty OAuth access token")
                serialized = serialize_oauth_bundle(renewed)
                if serialized != stored:
                    self.store.save(ref, serialized)
                self.environ[ref] = token
                self._states[ref] = "ready"
                self._next_check[ref] = self.clock() + 60
                return True
            except Exception as exc:
                # Never return SDK exception text or credential material.
                message = str(exc).lower()
                permanent = any(term in message for term in (
                    "invalid_grant", "refresh token revoked", "refresh token expired",
                    "invalid refresh token", "refresh token is invalid",
                ))
                self._states[ref] = "relogin_required" if permanent else "temporarily_unavailable"
                self._next_check[ref] = self.clock() + (300 if permanent else 30)
                raise ValidationError(self._safe_error(ref)) from None

    def _safe_error(self, ref: str) -> str:
        if self._states.get(ref) == "relogin_required":
            return "Kaggle OAuth permission was revoked or expired; reconnect this account in Control Plane."
        return "Kaggle OAuth refresh temporarily unavailable; Control Plane will check again automatically."

    def refresh_all(self) -> None:
        for ref in self.store.list_refs():
            if self._stop.is_set():
                return
            try:
                self.ensure(ref)
            except ValidationError:
                continue

    def start(self, interval=60.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            self.refresh_all()
            while not self._stop.wait(interval):
                self.refresh_all()
        self._thread = threading.Thread(target=run, name="kcp-oauth-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

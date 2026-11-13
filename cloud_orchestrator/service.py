"""First deployment is deliberately shadow-only; no submit-capable adapter exists here."""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time
from uuid import uuid4

from .protocol import decide
from .storage import FirestoreStore


def tick(store, clock=time.time, daily_limit=160, max_chains=12, observer=None):
    if not 1 <= daily_limit <= 160 or not 1 <= max_chains <= 12:
        raise ValueError("bounded daily ticks/chains required")
    now = clock()
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    owner = uuid4().hex
    fence = store.claim(owner, now, day, daily_limit)
    if fence is None:
        return {"mode": "shadow", "status": "SKIP_LEASE_OR_DAILY_LIMIT"}
    results = {}
    for row in store.list_chains(max_chains):
        chain_id = row.get("plan", {}).get("chain_id", "invalid")
        try:
            result = decide(row["plan"], row["state"], row.get("observation"),
                            row.get("validation"), clock())
            results[chain_id] = {"action": result.action, "reason": result.reason,
                                 "key": result.key, "at": clock(), "fence": fence}
        except (ValueError, KeyError, TypeError, OverflowError):
            # Avoid echoing request/plan/credential data in errors or logs.
            results["invalid" if not isinstance(chain_id, str) or "/" in chain_id else chain_id] = {
                "action": "STOP", "reason": "invalid chain document", "at": clock(), "fence": fence}
    monitored = {}
    if observer is not None:
        monitors = store.list_monitors(4)
        if monitors:
            monitored = observer.observe(monitors, deadline=now+45, clock=clock)
    if not store.publish(owner, fence, clock(), results, monitored):
        return {"mode": "shadow", "status": "LOST_LEASE", "count": len(results)}
    return {"mode": "shadow", "status": "RECORDED", "count": len(results),
            "monitored": len(monitored), "submission_enabled": False}


def make_handler(store, observer=None):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body):
            raw = json.dumps(body, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path != "/health":
                return self.respond(404, {"error": "not_found"})
            self.respond(200, {"mode": "shadow", "submission_enabled": False,
                               "source_sha256": os.environ.get("KCP_BUILD_SOURCE_SHA", "unsealed")})

        def do_POST(self):
            if self.path != "/tick":
                return self.respond(404, {"error": "not_found"})
            # Scheduler needs no payload; never accept credentials/commands over this API.
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                self.close_connection = True
                return self.respond(400, {"error": "empty_body_required"})
            try:
                self.respond(200, tick(store, observer=observer))
            except Exception:
                self.respond(503, {"error": "tick_unavailable"})

        def log_message(self, *_):
            pass
    return Handler


def main():
    if os.environ.get("KCP_CLOUD_MODE", "shadow") != "shadow":
        raise SystemExit("Live mode is not implemented or authorized for this release")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is required")
    store = FirestoreStore(project, os.environ.get("KCP_FIRESTORE_DATABASE", "(default)"))
    observer = None
    configurations = json.loads(os.environ.get("KCP_CLOUD_VAULTS", "[]"))
    if configurations:
        from .kaggle_monitor import GoogleKaggleSessions, KaggleObserver
        observer = KaggleObserver(GoogleKaggleSessions(configurations))
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), make_handler(store, observer)).serve_forever()


if __name__ == "__main__":
    main()

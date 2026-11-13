"""Durable, transaction-fenced ticks. No background workers or local SQLite."""
from __future__ import annotations

from copy import deepcopy
from threading import Lock


class MemoryStore:
    """CPU fixture backend; not allowed as a deployed durable store."""
    def __init__(self):
        self.lock = Lock()
        self.control = {"fence": 0, "lease_until": 0, "day": "", "ticks": 0}
        self.chains = []
        self.results = {}
        self.monitors = []
        self.monitor_results = {}

    def claim(self, owner, now, day, daily_limit):
        with self.lock:
            c = self.control
            if c["lease_until"] > now:
                return None
            used = c["ticks"] if c["day"] == day else 0
            if used >= daily_limit:
                return None
            c.update(owner=owner, fence=c["fence"] + 1, lease_until=now + 90,
                     day=day, ticks=used + 1)
            return c["fence"]

    def list_chains(self, limit):
        with self.lock:
            return deepcopy(self.chains[:limit])

    def list_monitors(self, limit):
        with self.lock:
            return deepcopy(self.monitors[:limit])

    def publish(self, owner, fence, now, results, monitor_results=None):
        with self.lock:
            c = self.control
            if c.get("owner") != owner or c["fence"] != fence or c["lease_until"] <= now:
                return False
            self.results = deepcopy(results)
            self.monitor_results = deepcopy(monitor_results or {})
            c["lease_until"] = 0
            return True


class FirestoreStore:
    def __init__(self, project, database="(default)"):
        from google.cloud import firestore
        self.fs = firestore
        self.db = firestore.Client(project=project, database=database)
        self.control = self.db.collection("kcp_cloud_control").document("tick")

    def claim(self, owner, now, day, daily_limit):
        @self.fs.transactional
        def claim_tx(tx):
            c = self.control.get(transaction=tx, retry=None, timeout=10).to_dict() or {}
            if c.get("lease_until", 0) > now:
                return None
            used = c.get("ticks", 0) if c.get("day") == day else 0
            if used >= daily_limit:
                return None
            fence = c.get("fence", 0) + 1
            tx.set(self.control, dict(owner=owner, fence=fence, lease_until=now + 90,
                                      day=day, ticks=used + 1))
            return fence
        return claim_tx(self.db.transaction(max_attempts=1))

    def list_chains(self, limit):
        return [s.to_dict() for s in self.db.collection("kcp_cloud_chains").limit(limit).stream(timeout=10, retry=None)]

    def list_monitors(self, limit):
        return [s.to_dict() for s in self.db.collection("kcp_cloud_monitors").limit(limit).stream(timeout=10, retry=None)]

    def publish(self, owner, fence, now, results, monitor_results=None):
        @self.fs.transactional
        def publish_tx(tx):
            c = self.control.get(transaction=tx, retry=None, timeout=10).to_dict() or {}
            if c.get("owner") != owner or c.get("fence") != fence or c.get("lease_until", 0) <= now:
                return False
            # Small summaries only. The untrusted full plan/validation is never logged.
            for chain_id, result in results.items():
                tx.set(self.db.collection("kcp_cloud_shadow").document(chain_id), result)
            for identity, result in (monitor_results or {}).items():
                tx.set(self.db.collection("kcp_cloud_status").document(identity), result)
            tx.update(self.control, {"lease_until": 0, "last_finished_at": now})
            return True
        return publish_tx(self.db.transaction(max_attempts=1))

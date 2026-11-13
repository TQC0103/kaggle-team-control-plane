"""Bounded private vault enrollment. Public plans/receipts never contain auth.

The desktop provider alone handles its existing credentials; the agent and
local API never receive them. Ambiguous writes require reconciliation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re

from .protocol import digest

PROJECT = "project-4a52875b-42ea-47e8-bc7"
PROJECT_NUMBER = "120421412307"
ACCOUNT = re.compile(r"acct_[a-f0-9]{32}\Z")
USERNAME = re.compile(r"[a-z0-9_-]{2,50}\Z")


def connection_plan(project, accounts):
    if project != PROJECT or not 1 <= len(accounts) <= 8:
        raise ValueError("unsupported project or account count")
    public = []
    for a in accounts:
        identity, username = a.get("id"), a.get("kaggle_username")
        if (not ACCOUNT.fullmatch(str(identity)) or not USERNAME.fullmatch(str(username))
                or a.get("state") != "enabled"):
            raise ValueError("invalid or disabled account")
        public.append({"id": identity, "username": username})
    public.sort(key=lambda x: x["id"])
    if len({a["id"] for a in public}) != len(public) or len({a["username"] for a in public}) != len(public):
        raise ValueError("duplicate account binding")
    tag = digest({"project": project, "accounts": public})[:12]
    vaults = [{"secret_id": f"kcp-kaggle-vault-{tag}-g{i//4+1:02}",
               "accounts": public[i:i+4]} for i in range(0, len(public), 4)]
    plan = dict(schema=1, project=project, accounts=public, vaults=vaults,
                retention_max_versions=2, purpose="authorized-research-orchestration",
                submission_enabled=False)
    return dict(plan, plan_sha256=digest(plan))


def validate_connection_plan(plan):
    try:
        regenerated = connection_plan(plan["project"], [dict(id=a["id"],
            kaggle_username=a["username"], state="enabled") for a in plan["accounts"]])
        if regenerated != plan:
            raise ValueError("modified connection plan")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("invalid connection plan") from None


class ReceiptJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.data, dict):
            raise ValueError("invalid receipt journal")
        for secret, row in self.data.items():
            if (not re.fullmatch(r"kcp-kaggle-vault-[a-f0-9]{12}-g0[12]", secret)
                    or not isinstance(row, dict)
                    or set(row)-{"status", "plan_sha256", "accounts", "version_name"}
                    or row.get("status") not in {"INTENT", "VERIFIED"}):
                raise ValueError("invalid receipt metadata")

    def mark(self, secret, state, **metadata):
        if set(metadata) - {"version_name", "plan_sha256", "accounts"}:
            raise ValueError("receipt metadata allowlist")
        self.data[secret] = dict(status=state, **metadata)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, sort_keys=True, indent=2)+"\n", encoding="utf-8")
        temporary.replace(self.path)


def connect(plan, provider, store, journal):
    validate_connection_plan(plan)
    outcomes = []
    for group in plan["vaults"]:
        sid = group["secret_id"]
        previous = journal.data.get(sid)
        if previous:
            if previous.get("plan_sha256") != plan["plan_sha256"]:
                raise ValueError("receipt plan mismatch")
            if previous.get("status") == "VERIFIED":
                expected_prefixes = tuple(f"projects/{p}/secrets/{sid}/versions/" for p in [plan['project'], PROJECT_NUMBER])
                if not str(previous.get("version_name", "")).startswith(expected_prefixes):
                    raise ValueError("receipt version identity mismatch")
                store.verify_version(previous["version_name"])
                outcomes.append(previous)
                continue
            raise ValueError("uncertain enrollment: reconcile metadata, never replay")
        if store.exists(plan["project"], sid):
            raise ValueError("existing vault: reconcile before enrollment")

        def deliver(items):
            if [(i.get("id"), i.get("username")) for i in items] != [(a["id"], a["username"]) for a in group["accounts"]]:
                raise ValueError("credential binding mismatch")
            for item in items:
                if item.get("kind") not in {"kaggle_oauth_v1", "kaggle_static_v1"} or not item.get("auth"):
                    raise ValueError("unsupported credential kind")
                if item["kind"] == "kaggle_oauth_v1" and item["auth"].get("username") != item["username"]:
                    raise ValueError("OAuth account mismatch")
            raw = json.dumps(dict(schema=1, plan_sha256=plan["plan_sha256"], accounts=items),
                             sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if len(raw) > 60000:
                raise ValueError("bounded vault payload exceeded")
            journal.mark(sid, "INTENT", plan_sha256=plan["plan_sha256"], accounts=group["accounts"])
            version = store.create_verified(plan["project"], sid, raw)
            journal.mark(sid, "VERIFIED", plan_sha256=plan["plan_sha256"],
                         accounts=group["accounts"], version_name=version)
            return {"status": "VERIFIED", "secret_id": sid, "version_name": version,
                    "accounts": group["accounts"]}

        # The provider calls deliver in-process; no credential-returning API exists.
        outcomes.append(provider.send(group["accounts"], deliver))
    return dict(status="CONNECTED", project=plan["project"], plan_sha256=plan["plan_sha256"],
                vaults=outcomes, submission_enabled=False)


class GoogleVaultStore:
    def __init__(self):
        from google.cloud import secretmanager
        self.client = secretmanager.SecretManagerServiceClient()

    def exists(self, project, secret):
        from google.api_core.exceptions import NotFound
        try:
            self.client.get_secret(request={"name": f"projects/{project}/secrets/{secret}"}, retry=None, timeout=15)
        except NotFound:
            return False
        return True

    def create_verified(self, project, secret, data):
        # Atomic creation is the claim. No append/retry on AlreadyExists/timeout.
        name = f"projects/{project}/secrets/{secret}"
        self.client.create_secret(request={"parent": f"projects/{project}", "secret_id": secret,
            "secret": {"replication": {"automatic": {}}, "labels": {"app": "kcp-cloud"}}},
            retry=None, timeout=15)
        version = self.client.add_secret_version(request={"parent": name, "payload": {"data": data}},
                                                  retry=None, timeout=20)
        readback = self.client.access_secret_version(request={"name": version.name}, retry=None, timeout=20)
        if not hmac.compare_digest(hashlib.sha256(bytes(readback.payload.data)).digest(), hashlib.sha256(data).digest()):
            raise ValueError("vault readback mismatch")
        return version.name

    def verify_version(self, name):
        from google.cloud.secretmanager_v1.types import SecretVersion
        version = self.client.get_secret_version(request={"name": name}, retry=None, timeout=15)
        if version.state != SecretVersion.State.ENABLED:
            raise ValueError("receipt vault version is not enabled")

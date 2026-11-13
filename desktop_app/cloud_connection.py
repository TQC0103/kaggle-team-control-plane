"""KCP maintenance CLI: reuse selected desktop sessions inside its auth boundary.

Planning reads only ordinary account metadata. Applying needs an exact reviewed
plan and Google ADC; it never starts/restarts the desktop app or its scheduler.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import urlopen

from cloud_orchestrator.connection import (
    PROJECT, GoogleVaultStore, ReceiptJournal, connect, connection_plan, validate_connection_plan,
)


def registry():
    with urlopen("http://127.0.0.1:8765/api/accounts", timeout=15) as response:
        return json.load(response)["accounts"]


class DesktopProvider:
    def __init__(self, runtime, accounts):
        self.runtime, self.accounts = runtime, {a["id"]: a for a in accounts}

    def send(self, accounts, consumer):
        bindings = []
        for expected in accounts:
            actual = self.accounts.get(expected["id"])
            if (not actual or actual["state"] != "enabled"
                    or actual["kaggle_username"] != expected["username"]):
                raise ValueError("live account binding changed")
            bindings.append(dict(id=actual["id"], username=actual["kaggle_username"],
                                 ref=actual["credential_env_ref"]))
        return self.runtime._auth_refresh.send_cloud_snapshot(bindings, consumer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", nargs="+", help="Exact existing KCP account IDs for a public plan")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--apply-plan", type=Path)
    parser.add_argument("--confirm-plan-sha")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        accounts = registry()
        if args.apply_plan:
            plan = json.loads(args.apply_plan.read_text(encoding="utf-8"))
            validate_connection_plan(plan)
            if args.confirm_plan_sha != plan["plan_sha256"] or not args.receipt:
                raise ValueError("exact plan confirmation and receipt required")
            selected = [a for a in accounts if a["id"] in {i["id"] for i in plan["accounts"]}]
            if connection_plan(PROJECT, selected) != plan:
                raise ValueError("live registry does not match reviewed plan")
            from .main import DesktopRuntime
            result = connect(plan, DesktopProvider(DesktopRuntime(), selected),
                             GoogleVaultStore(), ReceiptJournal(args.receipt))
        else:
            if not args.accounts or not args.out or len(set(args.accounts)) != len(args.accounts):
                raise ValueError("exact unique account IDs and public output required")
            selected = [a for a in accounts if a["id"] in args.accounts]
            if len(selected) != len(args.accounts):
                raise ValueError("unknown selected account")
            result = connection_plan(PROJECT, selected)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as f:
                json.dump(result, f, sort_keys=True, indent=2)
                f.write("\n")
        print(json.dumps(result, sort_keys=True))
    except BaseException as exc:
        # SDK exception text/tracebacks can contain private auth. Class only.
        raise SystemExit("CLOUD_CONNECTION_STOP_" + type(exc).__name__ + ": reconcile receipt; no automatic replay") from None


if __name__ == "__main__":
    main()

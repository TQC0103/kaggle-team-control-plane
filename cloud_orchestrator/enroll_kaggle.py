"""Optional one-account cloud onboarding; not part of the deployed shadow image.

Requires explicit user consent before execution. Uses fresh official Kaggle OAuth,
never imports desktop credentials. User enters the verification code privately.
Only public account/secret-version metadata is returned. No credential file saved.
"""
from __future__ import annotations

import argparse
import builtins
from contextlib import contextmanager
import getpass
import json
import logging
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal


def enroll(project, account, secret_id, store, login):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("invalid project ID")
    if not re.fullmatch(r"[a-z0-9_-]{2,50}", account):
        raise ValueError("invalid account name")
    if not re.fullmatch(r"kcp-kaggle-[a-z0-9-]{1,45}", secret_id):
        raise ValueError("invalid private secret name")
    # Metadata only; an existing credential is never fetched or overwritten.
    if store.has_versions(project, secret_id):
        raise ValueError("existing secret versions require separate reviewed update")
    actual_account, bundle = login()
    if actual_account.casefold() != account.casefold():
        raise ValueError("authenticated account identity mismatch")
    if bundle.get("kind") != "kaggle_oauth_v1" or bundle.get("username") != actual_account:
        raise ValueError("invalid OAuth result identity")
    if not bundle.get("refresh_token") or not bundle.get("access_token"):
        raise ValueError("incomplete OAuth result")
    data = json.dumps(bundle, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > 60000:
        raise ValueError("credential bundle exceeds bounded secret payload")
    version = store.add_initial(project, secret_id, data)
    return {"status": "ENROLLED", "project": project, "account": actual_account,
            "secret_id": secret_id, "version_name": version,
            "desktop_credentials_read": False, "credential_file_saved": False}


@contextmanager
def hidden_sdk_input():
    original = builtins.input
    builtins.input = lambda _prompt="": getpass.getpass("Kaggle verification code (hidden): ")
    try:
        yield
    finally:
        builtins.input = original


def fresh_oauth_login():
    from kagglesdk import KaggleEnv, KaggleOAuth
    from kagglesdk.kaggle_client import KaggleClient
    logging.getLogger("kagglesdk").setLevel(logging.CRITICAL)
    with KaggleClient(env=KaggleEnv.PROD, verbose=False) as client:
        oauth = KaggleOAuth(client=client)
        with hidden_sdk_input():
            credentials = oauth._run_oauth_flow(["resources.admin:*"], no_launch_browser=True)
        username = credentials.introspect()
        expiry = credentials._access_token_expiration
        bundle = {"kind": "kaggle_oauth_v1", "username": username,
                  "access_token": credentials._access_token,
                  "refresh_token": credentials._refresh_token,
                  "access_token_expiration": expiry.isoformat() if expiry else "",
                  "scopes": list(credentials._scopes or [])}
        # Do not call authenticate() or credentials.save(): those persist plaintext.
        return username, bundle


class SecretStore:
    def __init__(self):
        from google.cloud import secretmanager
        self.client = secretmanager.SecretManagerServiceClient()

    def has_versions(self, project, secret_id):
        from google.api_core.exceptions import NotFound
        name = f"projects/{project}/secrets/{secret_id}"
        try:
            self.client.get_secret(request={"name": name}, retry=None, timeout=10)
        except NotFound:
            return False
        return bool(list(self.client.list_secret_versions(request={"parent": name, "page_size": 1}, retry=None, timeout=10)))

    def add_initial(self, project, secret_id, data):
        from google.api_core.exceptions import AlreadyExists
        parent = f"projects/{project}"
        name = parent + "/secrets/" + secret_id
        try:
            self.client.create_secret(request={"parent": parent, "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}, "labels": {"app": "kcp-cloud"}}}, retry=None, timeout=10)
        except AlreadyExists:
            # Atomic creation is the enrollment claim. Never append to an existing
            # secret, even an empty one left by an uncertain earlier attempt.
            raise ValueError("existing secret requires metadata reconciliation") from None
        result = self.client.add_secret_version(request={"parent": name, "payload": {"data": data}}, retry=None, timeout=15)
        return result.name


def enrollment_deadline(_signal, _frame):
    raise TimeoutError("enrollment deadline")


def write_status(data):
    path = Path(".deploy/enrollment-status.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = dict(data, pid=os.getpid(), updated_at=datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--secret", default="kcp-kaggle-pilot")
    parser.add_argument("--consent", action="store_true")
    args = parser.parse_args()
    if not args.consent:
        raise SystemExit("STOP: explicit user consent required for fresh OAuth and private Secret Manager storage")
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, enrollment_deadline)
        signal.alarm(1800)
    write_status({"status": "WAITING_USER_OAUTH", "project": args.project, "account": args.account})
    try:
        result = enroll(args.project, args.account, args.secret, SecretStore(), fresh_oauth_login)
    except BaseException as exc:
        # Never print SDK exceptions/tracebacks that could contain credential material.
        write_status({"status": "STOP_SAFE", "error_class": type(exc).__name__,
                      "project": args.project, "account": args.account})
        raise SystemExit("ENROLLMENT_STOP (" + type(exc).__name__ + "): no credentials logged; reconcile metadata before retry") from None
    write_status(result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

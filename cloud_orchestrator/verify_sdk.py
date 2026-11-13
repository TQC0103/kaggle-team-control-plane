"""Normal Google SDK auth smoke test; no printed/exported auth or proxy process.

Uses the user's authorized ADC through google-auth. No credential-file reads,
gcloud print-token commands, public access or new IAM grants.
"""
from __future__ import annotations

import json
from pathlib import Path

from .connection import PROJECT
from .protocol import SHA

SERVICE_URL = "https://kcp-cloud-shadow-m34iwhe2va-uc.a.run.app"


def verify(source_sha, destination):
    import google.auth
    from google.auth.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    import requests

    if not SHA.fullmatch(source_sha):
        raise ValueError("invalid source seal")
    unauth = requests.get(SERVICE_URL+"/health", timeout=15, allow_redirects=False)
    if unauth.status_code not in {401, 403}:
        raise ValueError("service is not private")
    user, _ = google.auth.default()

    class DevelopmentIdentity(Credentials):
        def refresh(self, request):
            user.refresh(request)
            if not getattr(user, "id_token", None):
                raise ValueError("authorized Google user identity unavailable")
            self.token, self.expiry = user.id_token, user.expiry

    with AuthorizedSession(DevelopmentIdentity()) as session:
        health_response = session.get(SERVICE_URL+"/health", timeout=30, allow_redirects=False)
        if health_response.status_code != 200:
            raise ValueError("authenticated health request rejected")
        health = health_response.json()
        if health != dict(mode="shadow", submission_enabled=False, source_sha256=source_sha):
            raise ValueError("actual deployed source/mode mismatch")
        response = session.post(SERVICE_URL+"/tick", data=b"", timeout=60, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("actual cloud tick rejected")
        tick = response.json()
        if tick.get("mode") != "shadow" or tick.get("submission_enabled") is not False:
            raise ValueError("unexpected service mode")
        if tick.get("status") != "RECORDED" or tick.get("monitored") != 2:
            raise ValueError("cloud did not monitor both existing jobs")
    proof = dict(project=PROJECT, url=SERVICE_URL, source_sha256=source_sha,
                 unauthenticated_status=unauth.status_code, health=health, tick=tick,
                 authentication_backend="google-auth-user-identity", submission_enabled=False)
    Path(destination).write_text(json.dumps(proof, indent=2)+"\n", encoding="utf-8")
    return proof

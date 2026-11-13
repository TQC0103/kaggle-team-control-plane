"""Bounded real-cloud smoke test through gcloud's authenticated proxy.

Never prints or handles an access token. Never creates a Kaggle job or changes IAM.
"""
from __future__ import annotations

import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main(project, root=Path("."), gcloud_command="gcloud"):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Invalid project ID")
    state = root / ".deploy"
    service = json.loads((state / "service.json").read_text())
    url = service["status"]["url"]
    if not re.fullmatch(r"https://[a-z0-9.-]+\.run\.app", url):
        raise ValueError("Unexpected service URL")
    command = [gcloud_command, "--quiet", "--project=" + project]
    hidden = {"creationflags": 0x08000000} if os.name == "nt" else {}
    policy = subprocess.run(command + ["run", "services", "get-iam-policy", "kcp-cloud-shadow",
                                     "--region=us-central1", "--format=json"],
                            capture_output=True, text=True, timeout=30, check=True, **hidden)
    bindings = json.loads(policy.stdout).get("bindings", [])
    if any(m in {"allUsers", "allAuthenticatedUsers"} for b in bindings for m in b.get("members", [])):
        raise ValueError("Public service access is forbidden")
    try:
        with urlopen(url + "/health", timeout=15):
            raise ValueError("Unauthenticated request unexpectedly succeeded")
    except HTTPError as exc:
        if exc.code not in {401, 403}:
            raise ValueError("Unexpected unauthenticated response") from None
        unauthenticated_status = exc.code
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8081)) == 0:
            raise RuntimeError("Port8081 already occupied; reconcile existing proxy")
    expected_sha = hashlib.sha256((root / "SOURCE_MANIFEST.json").read_bytes()).hexdigest()
    with (state / "proxy.log").open("w") as output:
        proxy = subprocess.Popen(command + ["run", "services", "proxy", "kcp-cloud-shadow",
                                            "--region=us-central1", "--port=8081"],
                                 stdout=output, stderr=subprocess.STDOUT, **hidden)
        try:
            deadline = time.monotonic() + 50
            health = None
            while time.monotonic() < deadline:
                if proxy.poll() is not None:
                    raise RuntimeError("Authenticated proxy exited; inspect local proxy.log")
                try:
                    with urlopen("http://127.0.0.1:8081/health", timeout=5) as response:
                        health = json.load(response)
                        break
                except (URLError, TimeoutError):
                    time.sleep(1)
            if not health or health.get("source_sha256") != expected_sha:
                raise ValueError("Deployed source identity not verified")
            if health.get("mode") != "shadow" or health.get("submission_enabled") is not False:
                raise ValueError("Unexpected live/submission mode")
            req = Request("http://127.0.0.1:8081/tick", method="POST", data=b"")
            with urlopen(req, timeout=60) as response:
                result = json.load(response)
            if result.get("mode") != "shadow" or result.get("status") != "RECORDED" or result.get("submission_enabled") is not False:
                raise ValueError("Actual Firestore-backed tick did not pass")
            (state / "tick-pass.json").write_text(json.dumps(result, indent=2) + "\n")
            proof = {"project": project, "url": url, "source_sha256": expected_sha,
                     "unauthenticated_status": unauthenticated_status,
                     "public_iam": False, "health": health, "tick": result}
            (state / "verification.json").write_text(json.dumps(proof, indent=2) + "\n")
            print(json.dumps(proof, indent=2))
        finally:
            if proxy.poll() is None:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)


if __name__ == "__main__":
    import sys
    main(sys.argv[1])

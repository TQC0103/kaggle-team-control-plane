"""Read-only cloud preflight. Uses official CLI; never opens credential files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess


def check(project, run=subprocess.run, which=shutil.which):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("invalid Google Cloud project ID")
    result = {"project_id": project, "mode": "shadow", "ready_to_deploy": False,
              "cloud_mutations": 0, "blockers": []}
    cli = which("gcloud") or which("gcloud.cmd")
    if not cli:
        result["blockers"].append("GCLOUD_NOT_INSTALLED")
        return result
    commands = [
        ("project", [cli, "projects", "describe", project, "--format=json(projectId,lifecycleState)"]),
        ("billing", [cli, "billing", "projects", "describe", project,
                     "--format=json(projectId,billingEnabled)"]),
    ]
    for name, argv in commands:
        try:
            proc = run(argv, capture_output=True, text=True, timeout=30, check=False)
            if proc.returncode:
                result["blockers"].append(name.upper() + "_QUERY_FAILED")
                continue
            body = json.loads(proc.stdout)
            if body.get("projectId") != project:
                result["blockers"].append(name.upper() + "_IDENTITY_MISMATCH")
            elif name == "project" and body.get("lifecycleState") != "ACTIVE":
                result["blockers"].append("PROJECT_NOT_ACTIVE")
            elif name == "billing" and body.get("billingEnabled") is not True:
                result["blockers"].append("BILLING_NOT_ENABLED")
        except (subprocess.SubprocessError, OSError, ValueError):
            result["blockers"].append(name.upper() + "_QUERY_UNAVAILABLE")
    # Preflight cannot authorize IAM changes, migrate owners or claim free allowance.
    result["ready_to_deploy"] = not result["blockers"]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = check(args.project)
    raw = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(raw, encoding="utf-8")
    print(raw)
    return 0 if result["ready_to_deploy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

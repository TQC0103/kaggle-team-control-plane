"""Run a seeded, credential-free demo of the control plane.

This exercises the real database, scheduler, API, per-account isolation and
artifact flow, but replaces Kaggle network calls with the deterministic fake
adapter. It never reads a real Kaggle credential.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_plane.adapters import FakeKaggleAdapter
from control_plane.api import create_server
from control_plane.credentials import EnvCredentialVault
from control_plane.service import ControlPlaneService


OWNERS = (
    ("Minh", "demo-minh"),
    ("Lan", "demo-lan"),
    ("Huy", "demo-huy"),
    ("Mai", "demo-mai"),
    ("Nam", "demo-nam"),
    ("Anh", "demo-anh"),
    ("Thu", "demo-thu"),
    ("Binh", "demo-binh"),
    ("Linh", "demo-linh"),
    ("Khoa", "demo-khoa"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed and run the KCP fake demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="staging/artifact directory (defaults to work/demo-control-plane/data)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = PROJECT_ROOT
    demo_root = project_root / "work" / "demo-control-plane"
    demo_root.mkdir(parents=True, exist_ok=True)
    database_path = (args.db or demo_root / "demo.sqlite3").resolve()
    credential_values = {
        f"KCP_DEMO_ACCOUNT_{index:02d}": f"fake-demo-token-{index:02d}"
        for index in range(1, 11)
    }
    service = ControlPlaneService(
        database_path,
        data_dir=(args.data_dir or demo_root / "data").resolve(),
        adapter=FakeKaggleAdapter(poll_delay_seconds=0.2),
        vault=EnvCredentialVault(credential_values),
        max_workers=10,
        remote_poll_seconds=0.35,
        dispatch_poll_seconds=0.05,
        allowed_source_root=project_root,
    )

    try:
        accounts = service.list_accounts()
        if not accounts:
            for index, (owner, username) in enumerate(OWNERS, start=1):
                service.create_account(
                    {
                        "owner_name": owner,
                        "kaggle_username": username,
                        "credential_env_ref": f"KCP_DEMO_ACCOUNT_{index:02d}",
                        "consent_confirmed_by": owner,
                        "consent_note": "Synthetic account for the local MVP demo",
                        "weekly_quota_hours": 30,
                        "used_hours_estimate": round(index * 0.7, 1),
                    },
                    "demo-seeder",
                )
            accounts = service.list_accounts()

        if not service.list_batches():
            source_dir = project_root / "examples" / "kaggle-smoke-test"
            service.create_batch(
                {
                    "name": "round-1-parallel-demo",
                    "jobs": [
                        {
                            "account_id": account["id"],
                            "experiment_name": f"Parallel smoke test {index:02d}",
                            "source_dir": str(source_dir),
                            "kernel_slug": f"kcp-demo-{index:02d}",
                            "metadata": {
                                "accelerator": "cpu",
                                "fake_polls": 12 + index,
                                "fake_result": {
                                    "score": round(0.80 + index / 100, 3),
                                    "account": account["kaggle_username"],
                                },
                            },
                        }
                        for index, account in enumerate(accounts, start=1)
                    ],
                },
                "demo-seeder",
            )

        server = create_server(service, host=args.host, port=args.port)
        print(
            f"Seeded KCP demo listening on http://{args.host}:{args.port} "
            f"(database={database_path})",
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if "server" in locals():
            server.server_close()
        service.close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_interface.http_client import ApiClient
from agent_interface.tools import ToolRegistry
from control_plane.adapters import FakeKaggleAdapter
from control_plane.api import create_server
from control_plane.credentials import EnvCredentialVault
from control_plane.service import ControlPlaneService


class AgentContractTests(unittest.TestCase):
    def test_agent_tools_match_live_control_plane_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "experiment"
            source.mkdir()
            (source / "run.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "kernel-metadata.json").write_text(
                json.dumps(
                    {
                        "id": "placeholder/placeholder",
                        "title": "agent contract",
                        "code_file": "run.py",
                        "language": "python",
                        "kernel_type": "script",
                        "is_private": True,
                        "enable_gpu": False,
                        "enable_internet": False,
                        "dataset_sources": [],
                        "competition_sources": [],
                        "kernel_sources": [],
                    }
                ),
                encoding="utf-8",
            )
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=EnvCredentialVault({"TEAM_ONE": "fake-token"}),
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            account = service.create_account(
                {
                    "owner_name": "Owner One",
                    "kaggle_username": "team-one",
                    "credential_env_ref": "TEAM_ONE",
                    "consent_confirmed_by": "Owner One",
                    "weekly_quota_hours": 30,
                },
                "contract-test",
            )
            server = create_server(service, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            registry = ToolRegistry(
                ApiClient(f"http://127.0.0.1:{server.server_address[1]}")
            )
            try:
                accounts = registry.call("list_accounts")
                self.assertEqual(accounts["accounts"][0]["id"], account["id"])
                submitted = registry.call(
                    "submit_batch",
                    {
                        "name": "agent batch",
                        "experiments": [
                            {
                                "account_id": account["id"],
                                "experiment_name": "agent experiment",
                                "source_dir": str(source),
                                "kernel_slug": "agent-experiment",
                                "metadata": {"fake_result": {"score": 0.99}},
                            }
                        ],
                    },
                )
                job_id = submitted["batch"]["jobs"][0]["id"]
                deadline = time.monotonic() + 5
                result = {}
                while time.monotonic() < deadline:
                    result = registry.call("fetch_result", {"run_id": job_id})
                    if result["ready"]:
                        break
                    time.sleep(0.01)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["result"]["output"]["fake_result"]["score"], 0.99)
                self.assertTrue(result["events"])
                runs = registry.call("list_runs", {"account_id": account["id"]})
                self.assertEqual(runs["jobs"][0]["id"], job_id)
                audit = registry.call(
                    "audit_events", {"entity_type": "job", "entity_id": job_id}
                )
                self.assertTrue(audit["audit"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                service.close()


if __name__ == "__main__":
    unittest.main()

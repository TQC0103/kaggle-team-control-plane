import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("idle_installer", Path(__file__).resolve().parents[1] / "scripts/install_desktop_when_idle.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class DeploymentGateTests(unittest.TestCase):
    def test_chain_requires_every_validated_endpoint_and_analysis(self):
        state = dict(status="ANALYZED", authorized_run_ids=["C1", "C2b"], jobs=[
            dict(run="C1", stop=15000, artifact_status="VALIDATED")])
        self.assertFalse(installer.chain_finished(state))
        state["jobs"].append(dict(run="C2b", stop=15000, artifact_status="VALIDATED"))
        self.assertTrue(installer.chain_finished(state))
        state["status"] = "STOPPED_CHAIN_FAILURE"
        self.assertFalse(installer.chain_finished(state))

    def test_active_or_uncertain_work_blocks_install(self):
        account = dict(active_runs=0, remote_active_runs=0)
        self.assertTrue(installer.idle_snapshot([account], [], []))
        self.assertFalse(installer.idle_snapshot([], [], []))
        for field in ["active_runs", "remote_active_runs", "remote_reconciliation_required"]:
            self.assertFalse(installer.idle_snapshot([account | {field: 1}], [], []))
        for status in ["queued", "submitting", "submitted", "running", "cancel_requested"]:
            self.assertFalse(installer.idle_snapshot([account], [dict(status=status)], []))
        self.assertFalse(installer.idle_snapshot([account], [dict(status="succeeded", remote_may_be_running=1)], []))
        self.assertFalse(installer.idle_snapshot([account], [], [dict(status="running")]))

    def test_release_manifest_checks_every_file_and_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); exe = root / "KaggleControlPlane.exe"; exe.write_bytes(b"fixture")
            manifest = dict(release_files_sha256={exe.name: installer.sha(exe)})
            installer.verify_release(root, manifest)
            exe.write_bytes(b"changed")
            with self.assertRaises(ValueError):installer.verify_release(root, manifest)
            exe.write_bytes(b"fixture")
            manifest["release_files_sha256"]["../outside"] = "fixture"
            with self.assertRaises(ValueError):installer.verify_release(root, manifest)

    def test_powershell_literals_escape_single_quotes(self):
        self.assertEqual(installer.ps_literal("C:/somebody's app"), "'C:/somebody''s app'")

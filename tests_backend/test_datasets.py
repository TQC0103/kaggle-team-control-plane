from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.datasets import DatasetManager
from control_plane.api import create_server
from control_plane.errors import ConflictError, ValidationError
from control_plane.service import ControlPlaneService
from tests_backend.test_control_plane import account_payload


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "experiments" / "dataset"
        self.source.mkdir(parents=True)
        (self.source / "checkpoint.pth").write_bytes(b"test data")
        self.service = ControlPlaneService(self.root / "db.sqlite", data_dir=self.root / "internal", adapter_name="fake",
                                           allowed_source_root=self.root / "experiments", start_scheduler=False)
        self.account = self.service.create_account(account_payload("testowner", "KCP_TEST_DATASETS"), "test")
        self.calls = []
        self.remote = {"id": "testowner/sample-data", "title": "Sample dataset", "description": "before", "subtitle": "Keep me",
                       "isPrivate": True, "licenses": [{"name": "other"}], "collaborators": [],
                       "keywords": ["medical"], "resources": [{"path": "checkpoint.pth", "description": "Keep schema"}]}
        self.failure = ""
        self.visibility_mismatch = False
        self.block = None
        self.omit_defaults = False
        self.manager = DatasetManager(self.service, lambda account_id: (self.service.database.get_account(account_id), self.run_cli))
        self.selection = self.manager.prepare({"source_dir": str(self.source)})

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def run_cli(self, args, timeout=120):
        self.calls.append(args)
        if args[:2] == ["datasets", "create"]:
            if self.block:
                self.block.wait(3)
            if self.failure:
                return self.failure
            self.remote.update(json.loads((Path(args[args.index("-p") + 1]) / "dataset-metadata.json").read_text()))
            if self.visibility_mismatch:
                self.remote["isPrivate"] = not self.remote["isPrivate"]
            return "Dataset created"
        if args[:2] == ["datasets", "metadata"]:
            directory = Path(args[args.index("-p") + 1])
            if "--update" in args:
                self.remote = json.loads((directory / "dataset-metadata.json").read_text())
                if self.visibility_mismatch:
                    self.remote["isPrivate"] = not self.remote["isPrivate"]
            else:
                value = copy.deepcopy(self.remote)
                if self.omit_defaults:
                    if value.get("isPrivate") is False:
                        value.pop("isPrivate", None)
                    if not value.get("collaborators"):
                        value.pop("collaborators", None)
                (directory / "dataset-metadata.json").write_text(json.dumps(value))
            return "Metadata processed"
        if args[:2] == ["datasets", "files"]:
            return "name,size\ncheckpoint.pth,9\n"
        return "ready"

    def payload(self, **changes):
        return {"kind": "upload", "account_id": self.account["id"], "dataset_ref": "testowner/sample-data",
                "title": "Sample dataset", "selection_id": self.selection["selection_id"], "idempotency_key": "dataset-test-001", **changes}

    def wait(self, record):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.manager.get_operation(record["id"])
            if current["status"] in {"succeeded", "failed"}:
                # Wait for cleanup, which follows the terminal state update.
                directory = self.manager.root / "bundles" / record["id"]
                if not directory.exists():
                    return current
            time.sleep(.01)
        self.fail("operation did not complete")

    def test_private_default_hashes_files_and_preserves_original(self):
        result = self.wait(self.manager.submit(self.payload(readers=["friend01"]), "test"))
        self.assertEqual(result["status"], "succeeded", result)
        self.assertTrue(result["visibility_verified"])
        self.assertTrue(result["readers_verified"])
        self.assertEqual(len(result["files"][0]["sha256"]), 64)
        self.assertTrue(self.remote["isPrivate"])
        self.assertNotIn("--public", self.calls[0])
        self.assertEqual((self.source / "checkpoint.pth").read_bytes(), b"test data")

    def test_create_without_readers_skips_redundant_mutation_but_verifies_privacy(self):
        result = self.wait(self.manager.submit(self.payload(readers=[]), "test"))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(result['metadata_update_required'])
        self.assertFalse(any('--update' in c for c in self.calls))
        self.assertTrue(any(c[:2] == ['datasets','metadata'] for c in self.calls))
        self.assertTrue(result['visibility_verified'])
        self.assertTrue(result['readers_verified'])

    def test_create_with_readers_still_requires_permission_update(self):
        result = self.wait(self.manager.submit(self.payload(readers=['friend01']), 'test'))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(result['metadata_update_required'])
        self.assertTrue(any('--update' in c for c in self.calls))

    def test_public_requires_explicit_confirmation_and_correct_flag(self):
        with self.assertRaises(ValidationError):
            self.manager.submit(self.payload(is_private=False), "test")
        self.assertFalse(self.calls)
        result = self.wait(self.manager.submit(self.payload(is_private=False, confirm_public=True), "test"))
        self.assertEqual(result["status"], "succeeded", result)
        self.assertIn("--public", self.calls[0])
        self.assertFalse(self.remote["isPrivate"])

    def test_duplicate_submission_does_not_upload_twice_and_rejects_changed_request(self):
        payload = self.payload()
        record = self.wait(self.manager.submit(payload, "test"))
        self.assertEqual(self.manager.submit(payload, "test")["id"], record["id"])
        with self.assertRaises(ConflictError):
            self.manager.submit({**payload, "title": "Another dataset"}, "test")
        self.assertEqual(sum(c[:2] == ["datasets", "create"] for c in self.calls), 1)
        restarted = DatasetManager(self.service, lambda account_id: (self.account, self.run_cli))
        self.assertEqual(restarted.submit(payload, "test")["id"], record["id"])

    def test_upload_is_async_and_same_account_is_bounded(self):
        self.block = threading.Event()
        start = time.monotonic()
        record = self.manager.submit(self.payload(), "test")
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(len(self.manager.list_operations()), 1)
        with self.assertRaises(ConflictError):
            self.manager.submit(self.payload(idempotency_key="different-key"), "test")
        self.block.set()
        self.assertEqual(self.wait(record)["status"], "succeeded")

    def test_wrong_owner_and_credential_files_are_blocked_before_cli(self):
        with self.assertRaises(ValidationError):
            self.manager.submit(self.payload(dataset_ref="someoneelse/sample-data"), "test")
        for filename in ["kaggle.json", ".env", "credentials.json"]:
            file = self.source / filename
            file.write_text("do not read")
            with self.assertRaises(ValidationError):
                self.manager.select([file])
            file.unlink()
        with self.assertRaises(ValidationError):
            self.manager.prepare({"source_dir": str(self.root)})
        self.assertFalse(self.calls)

    def test_links_and_duplicate_names_are_blocked(self):
        with patch("pathlib.Path.is_symlink", return_value=True):
            with self.assertRaises(ValidationError):
                self.manager.select([self.source / "checkpoint.pth"])
        another = self.source / "sub"
        another.mkdir()
        (another / "checkpoint.pth").write_bytes(b"different")
        with self.assertRaises(ValidationError):
            self.manager.select([self.source / "checkpoint.pth", another / "checkpoint.pth"])
        selection = self.manager.select([self.source], folder=True)
        self.assertTrue(selection["nested_folders_zipped"])

    def test_exit_zero_creation_error_is_failure_and_does_not_update_settings(self):
        self.failure = "Dataset creation error: license rejected"
        result = self.wait(self.manager.submit(self.payload(), "test"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("license rejected", result["error"])
        self.assertFalse(any("--update" in c for c in self.calls))

    def test_visibility_must_be_verified(self):
        self.visibility_mismatch = True
        result = self.wait(self.manager.submit(self.payload(), "test"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("visibility differs", result["error"])

    def test_public_metadata_and_empty_permissions_can_omit_sdk_defaults(self):
        self.omit_defaults = True
        result = self.wait(self.manager.submit(self.payload(is_private=False, confirm_public=True), "test"))
        self.assertEqual(result["status"], "succeeded", result)
        remote = self.manager.inspect({"account_id": self.account["id"], "dataset_ref": "testowner/sample-data"})
        self.assertFalse(remote["is_private"])
        self.assertEqual(remote["readers"], [])

    def test_settings_preserve_schema_license_and_writer_roles(self):
        self.remote["collaborators"] = [{"username": "writer01", "role": "WRITER"}, {"username": "oldreader", "role": "READER"}]
        before = copy.deepcopy(self.remote)
        result = self.wait(self.manager.submit(self.payload(kind="settings", is_private=False, confirm_public=True,
                                                         readers=["newreader"], description="updated", license="CC0-1.0"), "test"))
        self.assertEqual(result["status"], "succeeded", result)
        for field in ["subtitle", "licenses", "keywords", "resources"]:
            self.assertEqual(self.remote[field], before[field])
        self.assertEqual(self.remote["description"], "updated")
        self.assertEqual(self.remote["collaborators"], [{"username": "newreader", "role": "READER"}, {"username": "writer01", "role": "WRITER"}])
        self.assertFalse(any(c[:2] == ["datasets", "create"] for c in self.calls))

    def test_restart_marks_uncertain_upload_interrupted_without_replay(self):
        record = {"id": "1" * 32, "status": "running", "phase": "Uploading", "created_at": "2026-10-02", "updated_at": "2026-10-02"}
        self.manager._save(record)
        restarted = DatasetManager(self.service, lambda account_id: (self.account, self.run_cli))
        self.assertEqual(restarted.get_operation(record["id"])["status"], "interrupted")
        self.assertFalse(self.calls)

    def test_changed_file_requires_reselection(self):
        (self.source / "checkpoint.pth").write_bytes(b"new data")
        result = self.wait(self.manager.submit(self.payload(), "test"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("changed", result["error"])
        self.assertFalse(self.calls)

    def test_http_upload_returns_202_and_history_while_cli_is_busy(self):
        self.service.datasets = self.manager
        self.block = threading.Event()
        server = create_server(self.service, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            client.request("POST", "/api/datasets/operations", json.dumps(self.payload()), {"Content-Type": "application/json"})
            response = client.getresponse()
            self.assertEqual(response.status, 202)
            record = json.loads(response.read())["operation"]
            client.request("GET", "/api/datasets/operations")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["operations"][0]["id"], record["id"])
        finally:
            self.block.set()
            self.wait(record)
            client.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

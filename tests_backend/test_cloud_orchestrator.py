import copy
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from types import SimpleNamespace
import subprocess
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest

from cloud_orchestrator.protocol import REQUIRED_CHECKS, decide, digest, validate_plan
from cloud_orchestrator.service import make_handler, tick
from cloud_orchestrator.storage import MemoryStore
from cloud_orchestrator.preflight import check
from cloud_orchestrator.package_shadow import package, SOURCE_FILES


def fixture():
    plan = dict(schema=1, chain_id="cpu_fixture", seed=1337, cap=2,
                scientific_sha256="a" * 64, no_test=True, no_sheet=True,
                max_iterations=15000, steps=[
                    dict(start=9600, stop=12000, source_sha256="b" * 64),
                    dict(start=12000, stop=15000, source_sha256="c" * 64)])
    state = dict(plan_sha256=digest(plan), owner_kind="cloud", handover_receipt="fixture-only",
                 step_index=0, used=1, receipt=dict(kernel_slug="fixture/parent", version=1),
                 admission=dict(account_id="fixture_account", observed_at=1000,
                                enabled=True, reconciled=True, active_jobs=1,
                                remaining_gpu_hours=0.5, input_read=True))
    obs = dict(ref="fixture/parent/1", status="complete", observed_at=1000)
    result = decide(plan, state, obs, None, 1000)
    validation = dict(key=result.key, parent_ref=obs["ref"], seed=1337, iteration=12000,
                      source_sha256="b" * 64, scientific_sha256="a" * 64,
                      checkpoint_sha256="d" * 64, status="PASS",
                      checks={k: True for k in REQUIRED_CHECKS})
    return plan, state, obs, validation


class CloudProtocolTests(unittest.TestCase):
    def test_ready_child_stable_key_and_no_input_mutation(self):
        p, s, o, v = fixture()
        original = copy.deepcopy((p, s, o, v))
        a, b = decide(p, s, o, v, 1000), decide(p, s, o, v, 1000)
        self.assertEqual(a.action, "READY_CHILD")
        self.assertEqual(a.key, b.key)
        self.assertEqual((p, s, o, v), original)

    def test_local_owner_never_handed_over_implicitly(self):
        p, s, o, v = fixture()
        for field, value in (("owner_kind", "local"), ("handover_receipt", None)):
            t = dict(s, **{field: value})
            self.assertEqual(decide(p, t, o, v, 1000).action, "WAIT_LOCAL_OWNER")

    def test_unknown_submission_always_reconciled(self):
        p, s, o, v = fixture()
        s["uncertain_intent"] = {"id": "fixture_intent"}
        self.assertEqual(decide(p, s, o, v, 1000).action, "RECONCILE")

    def test_missing_receipt_not_recreated(self):
        p, s, o, v = fixture()
        s.pop("receipt")
        self.assertEqual(decide(p, s, o, v, 1000).action, "RECONCILE")

    def test_source_plan_change_stops(self):
        p, s, o, v = fixture()
        p["steps"][1]["source_sha256"] = "f" * 64
        self.assertEqual(decide(p, s, o, v, 1000).action, "STOP")

    def test_exact_version_and_fresh_remote_required(self):
        p, s, o, v = fixture()
        for change in ({"ref": "fixture/parent/2"}, {"observed_at": 700},
                       {"observed_at": 1001}, {"status": "unknown"}):
            self.assertEqual(decide(p, s, dict(o, **change), v, 1000).action, "WAIT_REMOTE")

    def test_success_not_native_validation(self):
        p, s, o, _ = fixture()
        self.assertEqual(decide(p, s, o, None, 1000).action, "VALIDATE_PARENT")

    def test_missing_failed_or_nonboolean_native_checks_stop(self):
        p, s, o, v = fixture()
        for check in REQUIRED_CHECKS:
            for wrong in (False, "True", None):
                with self.subTest(check=check, wrong=wrong):
                    bad = copy.deepcopy(v)
                    bad["checks"][check] = wrong
                    self.assertEqual(decide(p, s, o, bad, 1000).action, "STOP")

    def test_validation_identity_must_match_all_seals(self):
        p, s, o, v = fixture()
        for field, value in (("seed", 2027), ("iteration", 9600),
                             ("parent_ref", "fixture/parent/2"),
                             ("source_sha256", "e" * 64), ("status", "FAIL"),
                             ("checkpoint_sha256", "missing")):
            self.assertEqual(decide(p, s, o, dict(v, **{field: value}), 1000).action, "STOP")

    def test_full_account_and_unknown_activity_block(self):
        p, s, o, v = fixture()
        for change in ({"active_jobs": 2}, {"active_jobs": -1}, {"active_jobs": None},
                       {"enabled": False}, {"remaining_gpu_hours": 0},
                       {"reconciled": False}, {"input_read": False},
                       {"observed_at": 500}, {"account_id": ""}):
            t = copy.deepcopy(s)
            t["admission"].update(change)
            self.assertEqual(decide(p, t, o, v, 1000).action, "WAIT_ACCOUNT")

    def test_caps_no_automatic_retry(self):
        p, s, o, v = fixture()
        s["used"] = 2
        self.assertEqual(decide(p, s, o, v, 1000).action, "STOP")
        for status in ("failed", "cancelled"):
            self.assertEqual(decide(p, s, dict(o, status=status), v, 1000).action, "STOP")

    def test_endpoint_done_requires_native_pass(self):
        p, s, o, v = fixture()
        s["step_index"] = 1
        v.update(iteration=15000, source_sha256="c" * 64,
                 key=decide(p, s, o, None, 1000).key)
        self.assertEqual(decide(p, s, o, v, 1000).action, "DONE")
        v["checks"]["rng"] = False
        self.assertEqual(decide(p, s, o, v, 1000).action, "STOP")

    def test_invalid_design_is_rejected(self):
        p, _, _, _ = fixture()
        for field, value in (("no_test", False), ("no_sheet", False),
                             ("cap", 1), ("max_iterations", 16000),
                             ("chain_id", "bad/path")):
            with self.assertRaises(ValueError):
                validate_plan(dict(p, **{field: value}))


class DurableTickTests(unittest.TestCase):
    def test_only_one_concurrent_lease_claim(self):
        store = MemoryStore()
        with ThreadPoolExecutor(max_workers=12) as pool:
            receipts = list(pool.map(lambda i: store.claim(str(i), 1000, "day", 160), range(12)))
        self.assertEqual(sum(x is not None for x in receipts), 1)

    def test_stale_owner_cannot_publish_after_lease_expiry(self):
        store = MemoryStore()
        old = store.claim("old", 1000, "day", 160)
        new = store.claim("new", 1091, "day", 160)
        self.assertGreater(new, old)
        self.assertFalse(store.publish("old", old, 1092, {"bad": {}}))
        self.assertTrue(store.publish("new", new, 1092, {"good": {}}))
        self.assertNotIn("bad", store.results)

    def test_daily_budget_stops_and_resets_without_expanding_limit(self):
        store = MemoryStore()
        f = store.claim("one", 1000, "day1", 1)
        self.assertTrue(store.publish("one", f, 1001, {}))
        self.assertIsNone(store.claim("two", 1002, "day1", 1))
        self.assertIsNotNone(store.claim("two", 90000, "day2", 1))

    def test_shadow_tick_publishes_decisions_without_submit(self):
        p, s, o, v = fixture()
        store = MemoryStore()
        store.chains = [dict(plan=p, state=s, observation=o, validation=v)]
        result = tick(store, clock=lambda: 1000)
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(store.results[p["chain_id"]]["action"], "READY_CHILD")
        self.assertEqual(s["used"], 1)

    def test_stale_evidence_cannot_be_refreshed_by_cloud_tick(self):
        p, s, o, v = fixture()
        store = MemoryStore()
        store.chains = [dict(plan=p, state=s, observation=o, validation=v)]
        tick(store, clock=lambda: 2000)
        self.assertEqual(store.results[p["chain_id"]]["action"], "WAIT_REMOTE")
        self.assertEqual(o["observed_at"], 1000)

    def test_malformed_chain_fails_closed(self):
        store = MemoryStore()
        store.chains = [dict(plan={"chain_id": "bad/path"})]
        tick(store, clock=lambda: 1000)
        self.assertEqual(store.results["invalid"]["action"], "STOP")

    def test_http_entry_empty_tick_and_no_body_commands(self):
        store = MemoryStore()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection(*server.server_address, timeout=3)
            conn.request("GET", "/health")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertFalse(json.loads(r.read())["submission_enabled"])
            conn.request("POST", "/tick", body=b"")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read())["mode"], "shadow")
            conn.request("POST", "/tick", body=b'{"command":"submit"}')
            r = conn.getresponse()
            self.assertEqual(r.status, 400)
            r.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class PreflightTests(unittest.TestCase):
    def test_missing_cli_has_no_cloud_mutations(self):
        result = check("research-example", which=lambda _: None)
        self.assertEqual(result["blockers"], ["GCLOUD_NOT_INSTALLED"])
        self.assertFalse(result["ready_to_deploy"])
        self.assertEqual(result["cloud_mutations"], 0)

    def test_unlinked_billing_blocks_and_only_read_commands_run(self):
        commands = []
        def run(argv, **_):
            commands.append(argv)
            body = {"projectId": "research-example"}
            body.update({"billingEnabled": False} if "billing" in argv else {"lifecycleState": "ACTIVE"})
            return SimpleNamespace(returncode=0, stdout=json.dumps(body))
        result = check("research-example", run=run, which=lambda _: "gcloud")
        self.assertEqual(result["blockers"], ["BILLING_NOT_ENABLED"])
        self.assertTrue(all("describe" in c for c in commands))

    def test_auth_timeout_has_sanitized_error(self):
        def run(argv, **_):
            raise subprocess.TimeoutExpired(argv, timeout=30, output="FAKE_SECRET_SENTINEL")
        result = check("research-example", run=run, which=lambda _: "gcloud")
        self.assertFalse(result["ready_to_deploy"])
        self.assertNotIn("FAKE_SECRET_SENTINEL", json.dumps(result))

    def test_project_identity_and_active_billing_required(self):
        def run(argv, **_):
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "projectId": "different-project", "lifecycleState": "ACTIVE", "billingEnabled": True}))
        result = check("research-example", run=run, which=lambda _: "gcloud")
        self.assertFalse(result["ready_to_deploy"])
        self.assertIn("PROJECT_IDENTITY_MISMATCH", result["blockers"])


class SourcePackagingTests(unittest.TestCase):
    def test_bundle_is_deterministic_and_allowlist_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            for name in SOURCE_FILES:
                (source / name).write_text("synthetic fixture " + name)
            (source / "not_for_upload.txt").write_text("fixture-only excluded text")
            a, b = package(source, root / "one.tar.gz"), package(source, root / "two.tar.gz")
            self.assertEqual(a["archive_sha256"], b["archive_sha256"])
            with tarfile.open(root / "one.tar.gz") as archive:
                self.assertEqual(len(archive.getnames()), len(SOURCE_FILES) + 1)
                self.assertFalse(any("not_for_upload" in name for name in archive.getnames()))
                self.assertTrue(all(name.startswith("kcp-cloud-shadow/") and ".." not in name
                                    for name in archive.getnames()))

    def test_existing_bundle_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            for name in SOURCE_FILES:
                (source / name).write_text("fixture " + name)
            destination = root / "bundle.tar.gz"
            package(source, destination)
            original = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                package(source, destination)
            self.assertEqual(destination.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

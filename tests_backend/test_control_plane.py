from __future__ import annotations

import gc
import http.client
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from control_plane.adapters import (
    AdapterError,
    FakeKaggleAdapter,
    KaggleCliAdapter,
    RemoteStatus,
    hidden_subprocess_kwargs,
    read_runtime_manifest,
)
from control_plane.api import create_server
from control_plane.credentials import EnvCredentialVault
from control_plane.errors import ConflictError, ValidationError
from control_plane.service import ControlPlaneService
from control_plane.scheduler import JobScheduler


def make_source(root: Path, name: str, original_id: str = "old-owner/old-kernel") -> Path:
    source = root / name
    source.mkdir()
    (source / "experiment.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "kernel-metadata.json").write_text(
        json.dumps(
            {
                "id": original_id,
                "id_no": 123456,
                "title": name,
                "code_file": "experiment.py",
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
    return source


def account_payload(username: str, env_ref: str) -> dict[str, object]:
    return {
        "owner_name": f"Owner {username}",
        "kaggle_username": username,
        "credential_env_ref": env_ref,
        "consent_confirmed_by": f"Owner {username}",
        "consent_note": "Team compute consent",
        "weekly_quota_hours": 30,
        "used_hours_estimate": 1.5,
    }


def wait_for_status(
    service: ControlPlaneService,
    job_id: str,
    expected: set[str],
    timeout: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        last = service.get_job(job_id)
        if last["status"] in expected:
            return last
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {expected}; last={last}")


class KaggleCliAdapterTests(unittest.TestCase):
    def test_runtime_manifest_is_bounded_and_allow_listed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "nested" / "runtime.json"
            runtime.parent.mkdir()
            runtime.write_text(
                json.dumps(
                    {
                        "python": "3.12.13",
                        "torch": "2.10.0+cu128",
                        "cuda_device": "Tesla T4",
                        "cuda_runtime": "12.8",
                        "secret": "must-not-leave-the-artifact",
                    }
                ),
                encoding="utf-8",
            )
            result = read_runtime_manifest(root)
        self.assertEqual(
            result,
            {
                "python": "3.12.13",
                "torch": "2.10.0+cu128",
                "cuda_device": "Tesla T4",
                "cuda_runtime": "12.8",
            },
        )

    @patch("control_plane.adapters.subprocess.STARTUPINFO")
    def test_windows_child_processes_are_hidden(self, startupinfo_factory) -> None:
        startupinfo = startupinfo_factory.return_value
        startupinfo.dwFlags = 0
        with patch("control_plane.adapters.subprocess.STARTF_USESHOWWINDOW", 1), patch(
            "control_plane.adapters.subprocess.SW_HIDE", 0
        ), patch("control_plane.adapters.subprocess.CREATE_NO_WINDOW", 0x08000000):
            options = hidden_subprocess_kwargs("nt")
        self.assertEqual(options["creationflags"], 0x08000000)
        self.assertEqual(startupinfo.dwFlags, 1)
        self.assertEqual(startupinfo.wShowWindow, 0)

    def test_non_windows_child_process_options_are_empty(self) -> None:
        self.assertEqual(hidden_subprocess_kwargs("posix"), {})

    def test_submit_passes_exact_machine_shape_to_kaggle_cli(self) -> None:
        adapter = KaggleCliAdapter()
        calls = []

        def fake_run(args, *_rest):
            calls.append(args)
            return (
                "Kernel version 1 successfully pushed. Please check progress at "
                "https://www.kaggle.com/code/expected-owner/expected-slug"
            )

        adapter._run = fake_run  # type: ignore[method-assign]
        adapter.submit(
            {
                "source_dir": "staged",
                "kernel_slug": "expected-owner/expected-slug",
                "metadata": {
                    "accelerator": "gpu",
                    "machine_shape": "NvidiaTeslaT4",
                },
            },
            {},
            threading.Event(),
        )
        self.assertEqual(
            calls,
            [[
                "kernels",
                "push",
                "-p",
                "staged",
                "--accelerator",
                "NvidiaTeslaT4",
            ]],
        )

    def test_diagnostics_uses_kernel_logs_without_downloading_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = KaggleCliAdapter()
            calls = []

            def fake_run(args, *_rest, **_kwargs):
                calls.append(args)
                return "Traceback: remote failure"

            adapter._run = fake_run  # type: ignore[method-assign]
            result = adapter.diagnostics(
                {
                    "output_dir": temporary,
                    "kernel_slug": "expected-owner/expected-slug",
                },
                {},
                threading.Event(),
            )
            self.assertEqual(
                calls, [["kernels", "logs", "expected-owner/expected-slug"]]
            )
            log_path = Path(temporary) / result["files"][0]
            self.assertEqual(
                log_path.read_text(encoding="utf-8").strip(),
                "Traceback: remote failure",
            )

    def test_live_logs_use_bounded_follow_stream(self) -> None:
        adapter = KaggleCliAdapter()
        calls = []

        def fake_follow(args, *_rest):
            calls.append(args)
            return "live line one\nlive line two"

        adapter._run_follow_snapshot = fake_follow  # type: ignore[method-assign]
        result = adapter.logs(
            {"kernel_slug": "expected-owner/expected-slug"},
            {},
            threading.Event(),
        )

        self.assertEqual(result, "live line one\nlive line two")
        self.assertEqual(
            calls,
            [["kernels", "logs", "--follow", "expected-owner/expected-slug"]],
        )

    def test_kaggle_json_log_envelope_is_rendered_as_web_visible_text(self) -> None:
        payload = json.dumps([
            {"stream_name": "stdout", "time": 1.0, "data": "epoch 1\\n"},
            {"stream_name": "stderr", "time": 2.0, "data": "warning\\n"},
        ])
        self.assertEqual(KaggleCliAdapter._kernel_log_text(payload), "epoch 1\\nwarning\\n")
        partial = payload[:-2]
        self.assertEqual(KaggleCliAdapter._kernel_log_text(partial), "epoch 1\\n")

    def test_live_log_snapshot_captures_unbuffered_partial_output(self) -> None:
        adapter = KaggleCliAdapter(
            executable=sys.executable,
            command_poll_seconds=0.01,
            live_log_capture_seconds=0.2,
        )
        output = adapter._run_follow_snapshot(
            ["-c", "import time; print('live partial', end=''); time.sleep(10)"],
            {},
            threading.Event(),
        )
        self.assertEqual(output, "live partial")

    def test_live_log_snapshot_does_not_wait_for_inherited_pipe(self) -> None:
        adapter = KaggleCliAdapter(
            executable=sys.executable,
            command_poll_seconds=0.01,
            live_log_capture_seconds=0.5,
        )
        started = time.monotonic()
        output = adapter._run_follow_snapshot(
            [
                "-c",
                (
                    "import subprocess, sys, time; "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.75)']); "
                    "print('parent output', flush=True); time.sleep(10)"
                ),
            ],
            {},
            threading.Event(),
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(output, "parent output")
        time.sleep(1.0)
        gc.collect()

    def test_submit_rejects_remote_owner_or_slug_drift(self) -> None:
        adapter = KaggleCliAdapter()
        adapter._run = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            "Kernel version 1 successfully pushed. Please check progress at "
            "https://www.kaggle.com/code/actual-owner/title-derived-slug"
        )
        with self.assertRaisesRegex(AdapterError, "actual-owner/title-derived-slug"):
            adapter.submit(
                {
                    "source_dir": "staged",
                    "kernel_slug": "expected-owner/expected-slug",
                },
                {},
                threading.Event(),
            )


class StorageAndCredentialsTests(unittest.TestCase):
    def test_job_runtime_fields_are_promoted_for_all_clients(self) -> None:
        decorated = ControlPlaneService._decorate_job(
            {
                "metadata": {
                    "accelerator": "gpu",
                    "machine_shape": "NvidiaTeslaT4",
                },
                "remote_started_at": "2026-08-21T00:00:00+00:00",
                "finished_at": "2026-08-21T00:02:03+00:00",
                "result": {
                    "output": {
                        "runtime": {
                            "python": "3.12.13",
                            "cuda_device": "Tesla T4",
                        }
                    }
                },
            }
        )
        self.assertEqual(decorated["accelerator"], "gpu")
        self.assertEqual(decorated["machine_shape"], "NvidiaTeslaT4")
        self.assertEqual(decorated["elapsed_seconds"], 123)
        self.assertEqual(
            decorated["runtime"],
            {"python": "3.12.13", "cuda_device": "Tesla T4"},
        )

    def test_modern_token_identity_is_introspected_without_returning_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = "modern-access-token-secret"
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"MODERN_ACCOUNT": token}),
                start_scheduler=False,
            )
            try:
                completed = __import__("subprocess").CompletedProcess(
                    ["kaggle", "config", "view"],
                    0,
                    stdout="Configuration values\n- username: detected-owner\n- auth_method: ACCESS_TOKEN\n",
                    stderr="",
                )
                with patch("control_plane.service.shutil.which", return_value="kaggle"), patch(
                    "control_plane.service.subprocess.run", return_value=completed
                ) as run:
                    identity = service.inspect_credential(
                        {"credential_env_ref": "MODERN_ACCOUNT"}
                    )
                self.assertEqual(identity["kaggle_username"], "detected-owner")
                self.assertNotIn(token, json.dumps(identity))
                self.assertEqual(run.call_args.kwargs["env"]["KAGGLE_API_TOKEN"], token)
            finally:
                service.close()

    def test_legacy_credential_identity_and_source_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready_source = make_source(root, "ready-source")
            (root / "plain-folder").mkdir()
            vault = EnvCredentialVault(
                {"TEAM_ACCOUNT": json.dumps({"username": "real-owner", "key": "secret"})}
            )
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                identity = service.inspect_credential(
                    {"credential_env_ref": "TEAM_ACCOUNT"}
                )
                self.assertEqual(identity["kaggle_username"], "real-owner")
                self.assertNotIn("secret", json.dumps(identity))
                listing = service.browse_sources()
                ready = next(
                    item for item in listing["directories"]
                    if item["path"] == str(ready_source.resolve())
                )
                self.assertTrue(ready["has_kernel_metadata"])
                selected = service.browse_sources(str(ready_source))
                self.assertTrue(selected["selectable"])
                with self.assertRaisesRegex(ValidationError, "allowed source root"):
                    service.browse_sources(str(root.parent))
            finally:
                service.close()

    def test_database_keeps_only_env_reference_and_child_env_is_minimal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = "super-secret-kaggle-token"
            vault = EnvCredentialVault({"TEAM_ACCOUNT_A": secret})
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("team-a", "TEAM_ACCOUNT_A"), "tester"
                )
                self.assertEqual(account["credential_env_ref"], "TEAM_ACCOUNT_A")
                self.assertTrue(account["credential_available"])
                database_bytes = (root / "state.sqlite3").read_bytes()
                self.assertNotIn(secret.encode(), database_bytes)

                with self.assertRaises(ValidationError):
                    service.create_account(
                        {
                            **account_payload("team-b", "TEAM_ACCOUNT_B"),
                            "token": "must-not-be-accepted",
                        },
                        "tester",
                    )

                os.environ["UNRELATED_AGENT_SECRET_FOR_TEST"] = "do-not-inherit"
                try:
                    child = vault.build_subprocess_env(
                        "TEAM_ACCOUNT_A", "team-a", root / "isolated"
                    )
                finally:
                    os.environ.pop("UNRELATED_AGENT_SECRET_FOR_TEST", None)
                self.assertEqual(child["KAGGLE_API_TOKEN"], secret)
                self.assertEqual(child["KAGGLE_USERNAME"], "team-a")
                self.assertNotIn("UNRELATED_AGENT_SECRET_FOR_TEST", child)
            finally:
                service.close()

    def test_legacy_lowercase_kaggle_json_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = EnvCredentialVault(
                {"LEGACY": json.dumps({"username": "legacy-user", "key": "legacy-key"})}
            )
            child = vault.build_subprocess_env(
                "LEGACY", "legacy-user", Path(temporary) / "config"
            )
            self.assertEqual(child["KAGGLE_USERNAME"], "legacy-user")
            self.assertEqual(child["KAGGLE_KEY"], "legacy-key")
            self.assertNotIn("KAGGLE_API_TOKEN", child)


class SchedulerTests(unittest.TestCase):
    def test_batch_submission_is_idempotent_and_detects_key_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"IDEMPOTENT_ACCOUNT": "token"}),
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("idempotent-user", "IDEMPOTENT_ACCOUNT"), "tester"
                )
                payload = {
                    "name": "idempotent batch",
                    "idempotency_key": "submit:client-request-001",
                    "jobs": [{
                        "account_id": account["id"],
                        "experiment_name": "idempotent experiment",
                        "source_dir": str(make_source(root, "idempotent-source")),
                        "kernel_slug": "idempotent-experiment",
                    }],
                }
                first = service.create_batch(payload, "tester")
                replay = service.create_batch(payload, "tester")
                self.assertEqual(replay["id"], first["id"])
                self.assertEqual(replay["jobs"][0]["id"], first["jobs"][0]["id"])
                changed = dict(payload)
                changed["name"] = "different request"
                with self.assertRaises(ConflictError):
                    service.create_batch(changed, "tester")
            finally:
                service.close()

    def test_support_bundle_is_allow_listed_and_contains_build_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = "must-never-appear-in-support-bundle"
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"SUPPORT_ACCOUNT": secret}),
                start_scheduler=False,
            )
            try:
                service.create_account(
                    account_payload("private-username", "SUPPORT_ACCOUNT"), "tester"
                )
                filename, bundle = service.support_bundle_download()
                self.assertEqual(filename, "kcp-support-bundle.zip")
                with zipfile.ZipFile(bundle) as archive:
                    self.assertEqual(archive.namelist(), ["support.json"])
                    raw = archive.read("support.json").decode("utf-8")
                    payload = json.loads(raw)
                self.assertIn("version", payload)
                self.assertIn("build_sha", payload)
                self.assertEqual(payload["account_summary"]["total"], 1)
                self.assertNotIn(secret, raw)
                self.assertNotIn("private-username", raw)
                self.assertNotIn("SUPPORT_ACCOUNT", raw)
            finally:
                service.close()

    def test_reaffirming_submitted_does_not_reset_remote_runtime_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"CLOCK_ACCOUNT": "token"}),
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("clock-user", "CLOCK_ACCOUNT"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "runtime clock",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "runtime clock",
                            "source_dir": str(make_source(root, "clock-source")),
                            "kernel_slug": "runtime-clock",
                        }],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                self.assertTrue(service.database.transition_job(job_id, {"queued"}, "submitting"))
                original_remote_started_at = "2026-08-25T00:00:00+00:00"
                self.assertTrue(service.database.transition_job(
                    job_id,
                    {"submitting"},
                    "submitted",
                    fields={"remote_started_at": original_remote_started_at},
                ))
                self.assertTrue(service.database.transition_job(
                    job_id, {"submitted"}, "submitted", fields={"remote_may_be_running": 1}
                ))
                self.assertEqual(
                    service.get_job(job_id)["remote_started_at"], original_remote_started_at
                )
                self.assertEqual(
                    sum(
                        event["message"] == "Job status changed to submitted"
                        for event in service.get_job(job_id)["events"]
                    ),
                    1,
                )
            finally:
                service.close()

    def test_live_log_reconnect_deduplicates_a_full_terminal_replay(self) -> None:
        previous = ["one", "two", "three"]
        current = ["zero", "one", "two", "three", "four"]
        lines, reset = JobScheduler._incremental_remote_log_lines(previous, current)
        self.assertEqual(lines, ["four"])
        self.assertFalse(reset)

    def test_live_log_text_deduplicates_a_replay_after_partial_progress_output(self) -> None:
        previous = "starting\nprogress 50%|████"
        current = "starting\nprogress 50%|██████\ncomplete\n"
        new_text, reset = JobScheduler._incremental_remote_log_text(previous, current)
        self.assertEqual(new_text, "██\ncomplete\n")
        self.assertFalse(reset)

    def test_live_log_mojibake_is_normalized_before_snapshot_matching(self) -> None:
        mojibake = "progress â–ˆâ–ˆâ–ˆ"
        self.assertEqual(JobScheduler._repair_utf8_mojibake(mojibake), "progress ███")
        previous = "progress ███"
        new_text, reset = JobScheduler._incremental_remote_log_text(
            previous, JobScheduler._repair_utf8_mojibake(mojibake) + "\ndone"
        )
        self.assertEqual(new_text, "\ndone")
        self.assertFalse(reset)

    def test_log_redaction_preserves_full_transcript_while_hiding_credentials(self) -> None:
        transcript = "first\n" + ("step\n" * 3000) + "secret-token\nlast\n"
        safe = JobScheduler._redact(
            transcript,
            {"KAGGLE_API_TOKEN": "secret-token"},
            max_text_chars=None,
        )
        self.assertNotIn("secret-token", safe)
        self.assertIn("[REDACTED]", safe)
        self.assertTrue(safe.endswith("last\n"))
        self.assertGreater(len(safe), 8000)

    def test_transient_status_error_after_submit_does_not_fabricate_failure(self) -> None:
        class FlakyStatusAdapter(FakeKaggleAdapter):
            def __init__(self) -> None:
                super().__init__(poll_delay_seconds=0.005)
                self.status_calls = 0

            def status(self, job, env, cancel_event):  # type: ignore[no-untyped-def]
                self.status_calls += 1
                if self.status_calls == 2:
                    raise AdapterError("temporary DNS lookup failure")
                return super().status(job, env, cancel_event)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = FlakyStatusAdapter()
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=adapter,
                vault=EnvCredentialVault({"FLAKY_ACCOUNT": "token"}),
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = service.create_account(
                    account_payload("flaky-user", "FLAKY_ACCOUNT"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "temporary status outage",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "survives local DNS outage",
                            "source_dir": str(make_source(root, "flaky-source")),
                            "kernel_slug": "flaky-status",
                            "metadata": {"fake_polls": 4},
                        }],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                completed = wait_for_status(service, job_id, {"succeeded"})
                self.assertFalse(completed["remote_may_be_running"])
                self.assertTrue(any(
                    event["message"] == "Could not query Kaggle status; keeping remote job active"
                    for event in completed["events"]
                ))
                self.assertFalse(any(
                    event["message"] == "Job status changed to failed"
                    for event in completed["events"]
                ))
            finally:
                service.close()

    def test_definite_submit_failure_does_not_create_remote_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "state.sqlite3"
            vault = EnvCredentialVault({"SUBMIT_ACCOUNT": "token"})
            first = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=vault,
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = first.create_account(
                    account_payload("submit-user", "SUBMIT_ACCOUNT"), "tester"
                )
                batch = first.create_batch(
                    {
                        "name": "definite submit failure",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "rejected before acceptance",
                            "source_dir": str(make_source(root, "submit-source")),
                            "kernel_slug": "rejected-submit",
                            "metadata": {
                                "fake_submit_error": "400 Client Error: Bad Request"
                            },
                        }],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                failed = wait_for_status(first, job_id, {"failed"})
                self.assertFalse(failed["remote_may_be_running"])
                with first.database.connection() as connection:
                    connection.execute(
                        "UPDATE jobs SET status='submitted',remote_may_be_running=1,"
                        "error='control plane restarted; reconciling remote Kaggle status',"
                        "finished_at=NULL WHERE id=?",
                        (job_id,),
                    )
            finally:
                first.close()

            second = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                self.assertEqual(second.recovered_jobs, 0)
                repaired = second.get_job(job_id)
                self.assertEqual(repaired["status"], "failed")
                self.assertFalse(repaired["remote_may_be_running"])
                self.assertTrue(any(
                    event["message"]
                    == "Repaired legacy submit failure; no remote Kaggle run was accepted"
                    for event in repaired["events"]
                ))
            finally:
                second.close()

    def test_remote_log_lines_survive_large_snapshot_and_paginate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"LOG_ACCOUNT": "token"}),
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("log-user", "LOG_ACCOUNT"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "remote log storage",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "log storage",
                            "source_dir": str(make_source(root, "log-source")),
                            "kernel_slug": "log-storage",
                        }],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                expected = [f"Kaggle line {index}" for index in range(600)]
                self.assertEqual(service.database.append_remote_log_lines(job_id, expected), 600)
                newest = service.job_remote_logs_page(job_id, limit=200)
                self.assertEqual([row["line"] for row in newest["logs"]], expected[-200:])
                self.assertTrue(newest["has_more"])
                detail = service.get_job(job_id)
                self.assertEqual(
                    [row["line"] for row in detail["remote_logs"]], expected[-500:]
                )
                self.assertTrue(detail["remote_logs_has_more"])
                self.assertEqual(
                    detail["remote_logs_before_id"], detail["remote_logs"][0]["sequence_id"]
                )
                older = service.job_remote_logs_page(
                    job_id, before_id=newest["before_id"], limit=200
                )
                self.assertEqual([row["line"] for row in older["logs"]], expected[-400:-200])
                terminal = ["Kaggle terminal line 1", "Kaggle terminal line 2"]
                self.assertEqual(service.database.replace_remote_log_lines(job_id, terminal), 2)
                replaced = service.job_remote_logs_page(job_id, limit=200)
                self.assertEqual([row["line"] for row in replaced["logs"]], terminal)
                self.assertFalse(replaced["has_more"])
            finally:
                service.close()

    def test_running_job_syncs_incremental_remote_logs_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "live-log-source")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=EnvCredentialVault({"LIVE_LOG_ACCOUNT": "secret-token"}),
                remote_poll_seconds=0.005,
                live_log_poll_seconds=0.001,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = service.create_account(
                    account_payload("live-log-user", "LIVE_LOG_ACCOUNT"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "live logs",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "live log experiment",
                            "source_dir": str(source),
                            "kernel_slug": "live-log-experiment",
                            "metadata": {
                                "accelerator": "cpu",
                                "fake_polls": 5,
                                "fake_live_logs": [
                                    "loading dataset",
                                    "secret-token must be redacted",
                                    "evaluation complete",
                                ],
                            },
                        }],
                    },
                    "tester",
                )
                job = wait_for_status(service, batch["jobs"][0]["id"], {"succeeded"})
                synced = [
                    entry["line"]
                    for entry in job["remote_logs"]
                ]
                self.assertEqual(
                    synced,
                    ["loading dataset", "[REDACTED] must be redacted", "evaluation complete"],
                )
            finally:
                service.close()

    def test_failed_job_download_includes_redacted_kaggle_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "failed-source")
            secret = "remote-log-secret-token"
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=EnvCredentialVault({"FAILED_ACCOUNT": secret}),
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = service.create_account(
                    account_payload("failed-user", "FAILED_ACCOUNT"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "failed diagnostics",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "failed experiment",
                            "source_dir": str(source),
                            "kernel_slug": "failed-experiment",
                            "metadata": {
                                "accelerator": "cpu",
                                "fake_outcome": "failed",
                                "fake_remote_log": (
                                    f"Traceback (most recent call last): {secret}"
                                ),
                            },
                        }],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                failed = wait_for_status(service, job_id, {"failed"})
                self.assertIn("failure_output", failed["result"])
                self.assertEqual(
                    [entry["line"] for entry in failed["remote_logs"]],
                    ["Traceback (most recent call last): [REDACTED]"],
                )
                output_dir = Path(failed["output_dir"])
                for existing_log in output_dir.glob("*.log"):
                    existing_log.unlink()

                _name, log_path = service.job_logs_download(job_id)
                log_text = log_path.read_text(encoding="utf-8")
                self.assertIn("=== Kaggle remote log:", log_text)
                self.assertIn("Traceback (most recent call last)", log_text)
                self.assertIn("[REDACTED]", log_text)
                self.assertNotIn(secret, log_text)
            finally:
                service.close()

    def test_logs_paginate_and_result_zip_contains_manifest_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "download-source")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=EnvCredentialVault({"DOWNLOAD_ACCOUNT": "token"}),
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = service.create_account(
                    account_payload("download-user", "DOWNLOAD_ACCOUNT"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "downloads",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "download experiment",
                            "source_dir": str(source),
                            "kernel_slug": "download-experiment",
                            "metadata": {"accelerator": "cpu", "fake_result": {"score": 0.99}},
                        }],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                wait_for_status(service, job_id, {"succeeded"})
                for index in range(250):
                    service.database.append_job_event(job_id, f"extra log {index}")

                first = service.job_events_page(job_id, limit=200)
                self.assertEqual(len(first["events"]), 200)
                self.assertTrue(first["has_more"])
                older = service.job_events_page(
                    job_id, before_id=first["before_id"], limit=200
                )
                self.assertTrue(older["events"])
                self.assertLess(
                    older["events"][-1]["sequence_id"],
                    first["events"][0]["sequence_id"],
                )

                log_name, log_path = service.job_logs_download(job_id)
                self.assertTrue(log_name.endswith("-logs.log"))
                log_bytes = log_path.read_bytes()
                self.assertIn(b"extra log 0", log_bytes)
                self.assertIn(b"extra log 249", log_bytes)

                zip_name, zip_path = service.job_result_download(job_id)
                self.assertTrue(zip_name.endswith("-results.zip"))
                with zipfile.ZipFile(zip_path) as archive:
                    self.assertIn("job-result.json", archive.namelist())
                    self.assertIn("fake-result.json", archive.namelist())
                    manifest = json.loads(archive.read("job-result.json"))
                self.assertEqual(manifest["job_id"], job_id)
                self.assertEqual(manifest["result"]["output"]["fake_result"]["score"], 0.99)

                server = create_server(service, port=0)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_address[1], timeout=10
                    )
                    connection.request("GET", f"/api/jobs/{job_id}/logs/download")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "text/plain; charset=utf-8")
                    self.assertIn("-logs.log", response.getheader("Content-Disposition"))
                    self.assertIn(b"extra log 249", response.read())
                    connection.close()

                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_address[1], timeout=10
                    )
                    connection.request("GET", f"/api/jobs/{job_id}/result/download")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "application/zip")
                    archive_bytes = response.read()
                    connection.close()
                    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as downloaded:
                        self.assertIn("fake-result.json", downloaded.namelist())
                finally:
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)
            finally:
                service.close()

    def test_two_accounts_run_concurrently_with_isolated_staging_and_envs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_a = make_source(root, "source-a")
            source_b = make_source(root, "source-b")
            adapter = FakeKaggleAdapter(poll_delay_seconds=0.005)
            vault = EnvCredentialVault(
                {
                    "ACCOUNT_A_TOKEN": "token-a",
                    "ACCOUNT_B_TOKEN": json.dumps(
                        {"username": "team-b", "key": "key-b"}
                    ),
                }
            )
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=adapter,
                vault=vault,
                max_workers=2,
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            try:
                account_a = service.create_account(
                    account_payload("team-a", "ACCOUNT_A_TOKEN"), "tester"
                )
                account_b = service.create_account(
                    account_payload("team-b", "ACCOUNT_B_TOKEN"), "tester"
                )
                service.sync_account_quota(account_a["id"])
                service.sync_account_quota(account_b["id"])
                batch = service.create_batch(
                    {
                        "name": "parallel test",
                        "jobs": [
                            {
                                "account_id": account_a["id"],
                                "experiment_name": "A",
                                "source_dir": str(source_a),
                                "kernel_slug": "experiment-a",
                                "metadata": {
                                    "fake_submit_delay": 0.35,
                                    "fake_result": {"score": 0.91},
                                    "accelerator": "gpu",
                                },
                            },
                            {
                                "account_id": account_b["id"],
                                "experiment_name": "B",
                                "source_dir": str(source_b),
                                "kernel_slug": "team-b/experiment-b",
                                "metadata": {
                                    "fake_submit_delay": 0.35,
                                    "fake_result": {"score": 0.92},
                                },
                            },
                        ],
                    },
                    "tester",
                )
                jobs = batch["jobs"]
                completed = [
                    wait_for_status(service, job["id"], {"succeeded"}) for job in jobs
                ]
                self.assertEqual(adapter.max_in_flight, 2)
                self.assertEqual(
                    {job["account_id"] for job in completed},
                    {account_a["id"], account_b["id"]},
                )
                self.assertNotEqual(
                    adapter.submitted_env_markers[jobs[0]["id"]]["KAGGLE_CONFIG_DIR"],
                    adapter.submitted_env_markers[jobs[1]["id"]]["KAGGLE_CONFIG_DIR"],
                )
                jobs_by_account = {job["account_id"]: job for job in jobs}
                job_a = jobs_by_account[account_a["id"]]
                job_b = jobs_by_account[account_b["id"]]
                staged_a = json.loads(
                    (root / "data" / "staging" / job_a["id"] / "kernel-metadata.json").read_text(
                        encoding="utf-8"
                    )
                )
                staged_b = json.loads(
                    (root / "data" / "staging" / job_b["id"] / "kernel-metadata.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(staged_a["id"], "team-a/experiment-a")
                self.assertEqual(staged_b["id"], "team-b/experiment-b")
                self.assertEqual(staged_a["title"], "experiment-a")
                self.assertEqual(staged_b["title"], "experiment-b")
                self.assertNotIn("id_no", staged_a)
                self.assertNotIn("id_no", staged_b)
                self.assertTrue(staged_a["enable_gpu"])
                self.assertFalse(staged_a["enable_tpu"])
                self.assertEqual(staged_a["machine_shape"], "NvidiaTeslaT4")
                original = json.loads(
                    (source_a / "kernel-metadata.json").read_text(encoding="utf-8")
                )
                self.assertEqual(original["id"], "old-owner/old-kernel")
                result = service.job_result(job_a["id"])
                self.assertTrue(result["ready"])
                self.assertEqual(
                    result["result"]["output"]["fake_result"]["score"], 0.91
                )
                detailed = service.get_job(job_a["id"])
                self.assertTrue(
                    any("remote status" in event["message"] for event in detailed["events"])
                )
                synced = service.sync_account_quota(account_a["id"])["account"]
                self.assertEqual(
                    synced["official_quota"]["gpu"]["used_hours"], 1.0
                )
                self.assertNotIn("used_hours_estimate", synced)
            finally:
                service.close()

    def test_same_account_runs_at_most_two_jobs_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_a = make_source(root, "same-a")
            source_b = make_source(root, "same-b")
            source_c = make_source(root, "same-c")
            adapter = FakeKaggleAdapter(poll_delay_seconds=0.005)
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=adapter,
                vault=EnvCredentialVault({"ONE_ACCOUNT": "token"}),
                max_workers=2,
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = service.create_account(
                    account_payload("one-user", "ONE_ACCOUNT"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "same account bounded parallelism",
                        "jobs": [
                            {
                                "account_id": account["id"],
                                "experiment_name": "one",
                                "source_dir": str(source_a),
                                "kernel_slug": "same-one",
                                "metadata": {"fake_submit_delay": 0.1},
                            },
                            {
                                "account_id": account["id"],
                                "experiment_name": "three",
                                "source_dir": str(source_c),
                                "kernel_slug": "same-three",
                                "metadata": {"fake_submit_delay": 0.1},
                            },
                            {
                                "account_id": account["id"],
                                "experiment_name": "two",
                                "source_dir": str(source_b),
                                "kernel_slug": "same-two",
                                "metadata": {"fake_submit_delay": 0.1},
                            },
                        ],
                    },
                    "tester",
                )
                for job in batch["jobs"]:
                    wait_for_status(service, job["id"], {"succeeded"})
                self.assertEqual(adapter.max_in_flight, 2)
            finally:
                service.close()

    def test_batch_cap_and_managed_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "source")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"ACCOUNT_TOKEN": "token"}),
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("team-user", "ACCOUNT_TOKEN"), "tester"
                )
                base_job = {
                    "account_id": account["id"],
                    "experiment_name": "cap",
                    "source_dir": str(source),
                    "kernel_slug": "cap-job",
                }
                with self.assertRaisesRegex(ValidationError, "at most 10"):
                    service.create_batch(
                        {"name": "too many", "jobs": [base_job] * 11}, "tester"
                    )
                with self.assertRaisesRegex(ValidationError, "custom paths"):
                    service.create_batch(
                        {
                            "name": "unsafe output",
                            "jobs": [{**base_job, "output_dir": str(root / "outside")}],
                        },
                        "tester",
                    )
                with self.assertRaisesRegex(ValidationError, "accelerator"):
                    service.create_batch(
                        {
                            "name": "bad accelerator",
                            "jobs": [
                                {**base_job, "metadata": {"accelerator": "quantum"}}
                            ],
                        },
                        "tester",
                    )
                with self.assertRaisesRegex(ValidationError, "machine_shape"):
                    service.create_batch(
                        {
                            "name": "unsafe GPU shape",
                            "jobs": [
                                {
                                    **base_job,
                                    "metadata": {
                                        "accelerator": "gpu",
                                        "machine_shape": "NvidiaTeslaP100",
                                    },
                                }
                            ],
                        },
                        "tester",
                    )
            finally:
                service.close()

    def test_source_must_stay_in_allowed_root_and_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = make_source(root, "outside")
            safe_source = make_source(allowed, "safe")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=allowed,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"ACCOUNT_TOKEN": "token"}),
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("team-user", "ACCOUNT_TOKEN"), "tester"
                )
                base = {
                    "account_id": account["id"],
                    "experiment_name": "source safety",
                    "kernel_slug": "source-safety",
                }
                with self.assertRaisesRegex(ValidationError, "allowed source root"):
                    service.create_batch(
                        {
                            "name": "outside",
                            "jobs": [{**base, "source_dir": str(outside)}],
                        },
                        "tester",
                    )

                external_data = root / "external.txt"
                external_data.write_text("private", encoding="utf-8")
                link = safe_source / "linked-secret.txt"
                try:
                    link.symlink_to(external_data)
                except OSError:
                    self.skipTest("symbolic links are unavailable on this Windows host")
                batch = service.create_batch(
                    {
                        "name": "symlink",
                        "jobs": [{**base, "source_dir": str(safe_source)}],
                    },
                    "tester",
                )
                service.scheduler.start()
                failed = wait_for_status(
                    service, batch["jobs"][0]["id"], {"failed"}
                )
                self.assertIn("symbolic links", failed["error"])
            finally:
                service.close()

    def test_cancel_running_stops_local_monitor_and_retry_keeps_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "source")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=EnvCredentialVault({"ACCOUNT_TOKEN": "token"}),
                max_workers=1,
                remote_poll_seconds=0.01,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = service.create_account(
                    account_payload("team-user", "ACCOUNT_TOKEN"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "cancel test",
                        "jobs": [
                            {
                                "account_id": account["id"],
                                "experiment_name": "long",
                                "source_dir": str(source),
                                "kernel_slug": "long-run",
                                "metadata": {"fake_polls": 1000},
                            }
                        ],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                wait_for_status(service, job_id, {"running"})
                cancellation = service.cancel_job(job_id, "tester")
                self.assertEqual(
                    cancellation["cancel_semantics"], "local_monitor_stop_requested"
                )
                cancelled = wait_for_status(service, job_id, {"cancelled"})
                self.assertTrue(cancelled["remote_may_be_running"])
                blocked_account = service.get_account(account["id"])
                self.assertTrue(blocked_account["remote_reconciliation_required"])
                with self.assertRaisesRegex(ConflictError, "reconciliation"):
                    service.retry_job(job_id, "tester")
                with self.assertRaisesRegex(ConflictError, "reconciliation"):
                    service.create_batch(
                        {
                            "name": "blocked until reconciled",
                            "jobs": [
                                {
                                    "account_id": account["id"],
                                    "experiment_name": "blocked",
                                    "source_dir": str(source),
                                    "kernel_slug": "blocked-run",
                                }
                            ],
                        },
                        "tester",
                    )
                with self.assertRaisesRegex(ValidationError, "confirmed"):
                    service.reconcile_account(
                        account["id"], {"confirmed": False}, "tester"
                    )
                reconciled = service.reconcile_account(
                    account["id"],
                    {"confirmed": True, "note": "Verified stopped on Kaggle"},
                    "tester",
                )
                self.assertEqual(reconciled["reconciled_job_count"], 1)
                self.assertFalse(
                    reconciled["account"]["remote_reconciliation_required"]
                )
                retried = service.retry_job(job_id, "tester")
                self.assertEqual(retried["account_id"], account["id"])
                self.assertEqual(retried["retry_of_job_id"], job_id)
                self.assertEqual(retried["attempt"], 2)
            finally:
                service.close()

    def test_restart_automatically_reconciles_cancelled_remote_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "cancelled-recovery-source")
            database_path = root / "state.sqlite3"
            vault = EnvCredentialVault({"ACCOUNT_TOKEN": "token"})
            first = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                account = first.create_account(
                    account_payload("cancelled-recovery-user", "ACCOUNT_TOKEN"), "tester"
                )
                batch = first.create_batch(
                    {
                        "name": "cancelled remote uncertainty",
                        "jobs": [
                            {
                                "account_id": account["id"],
                                "experiment_name": "cancelled locally",
                                "source_dir": str(source),
                                "kernel_slug": "cancelled-recovery",
                                "metadata": {"fake_polls": 1},
                            }
                        ],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                self.assertTrue(
                    first.database.transition_job(
                        job_id,
                        {"queued"},
                        "cancelled",
                        fields={"remote_may_be_running": 1},
                    )
                )
            finally:
                first.close()

            second = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                self.assertEqual(second.recovered_jobs, 1)
                recovered = second.get_job(job_id)
                self.assertEqual(recovered["status"], "succeeded")
                self.assertFalse(recovered["remote_may_be_running"])
                account_state = second.get_account(account["id"])
                self.assertFalse(account_state["remote_reconciliation_required"])
            finally:
                second.close()

    def test_restart_preserves_running_remote_job_and_blocks_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "restart-source")
            database_path = root / "state.sqlite3"
            vault = EnvCredentialVault({"ACCOUNT_TOKEN": "token"})
            first = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            account = first.create_account(
                account_payload("restart-user", "ACCOUNT_TOKEN"), "tester"
            )
            batch = first.create_batch(
                {
                    "name": "interrupted",
                    "jobs": [
                        {
                            "account_id": account["id"],
                            "experiment_name": "interrupted",
                            "source_dir": str(source),
                            "kernel_slug": "interrupted-run",
                        }
                    ],
                },
                "tester",
            )
            job_id = batch["jobs"][0]["id"]
            self.assertTrue(
                first.database.transition_job(job_id, {"queued"}, "submitting")
            )
            self.assertTrue(
                first.database.transition_job(job_id, {"submitting"}, "submitted")
            )
            first.close()

            second = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                self.assertEqual(second.recovered_jobs, 1)
                recovered = second.get_job(job_id)
                self.assertEqual(recovered["status"], "running")
                self.assertTrue(recovered["remote_may_be_running"])
                self.assertTrue(
                    second.get_account(account["id"])[
                        "remote_reconciliation_required"
                    ]
                )
                with self.assertRaisesRegex(ConflictError, "reconciliation"):
                    second.create_batch(
                        {
                            "name": "blocked after restart",
                            "jobs": [
                                {
                                    "account_id": account["id"],
                                    "experiment_name": "blocked",
                                    "source_dir": str(source),
                                    "kernel_slug": "blocked-after-restart",
                                }
                            ],
                        },
                        "tester",
                    )
                self.assertIn(
                    "Kaggle remote status after restart: running",
                    "\n".join(event["message"] for event in recovered["events"]),
                )
            finally:
                second.close()

    def test_recovery_recheck_does_not_repeat_an_unchanged_remote_state(self) -> None:
        class StaticQueuedAdapter(FakeKaggleAdapter):
            def status(self, job, env, cancel_event):  # type: ignore[no-untyped-def]
                return RemoteStatus("queued", "Kaggle still reports queued")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=StaticQueuedAdapter(),
                vault=EnvCredentialVault({"ACCOUNT_TOKEN": "token"}),
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("quiet-recovery-user", "ACCOUNT_TOKEN"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "quiet recovery",
                        "jobs": [{
                            "account_id": account["id"],
                            "experiment_name": "queued recovery",
                            "source_dir": str(make_source(root, "quiet-recovery-source")),
                            "kernel_slug": "quiet-recovery",
                        }],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                self.assertTrue(service.database.transition_job(job_id, {"queued"}, "submitting"))
                self.assertTrue(service.database.transition_job(job_id, {"submitting"}, "submitted"))

                service._reconcile_recovered_jobs([job_id])
                service._reconcile_recovered_jobs([job_id], announce_restart=False)
                service._reconcile_recovered_jobs([job_id], announce_restart=False)

                messages = [event["message"] for event in service.get_job(job_id)["events"]]
                self.assertEqual(messages.count("Kaggle remote status after restart: queued"), 1)
                self.assertNotIn("Kaggle remote status changed: queued", messages)
            finally:
                service.close()

    def test_app_shutdown_preserves_remote_job_for_restart_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "state.sqlite3"
            adapter = FakeKaggleAdapter(poll_delay_seconds=0.005)
            vault = EnvCredentialVault({"ACCOUNT_TOKEN": "token"})
            first = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=adapter,
                vault=vault,
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            account = first.create_account(
                account_payload("shutdown-user", "ACCOUNT_TOKEN"), "tester"
            )
            batch = first.create_batch(
                {
                    "name": "shutdown recovery",
                    "jobs": [{
                        "account_id": account["id"],
                        "experiment_name": "remote survives desktop close",
                        "source_dir": str(make_source(root, "shutdown-source")),
                        "kernel_slug": "shutdown-recovery",
                        "metadata": {"fake_polls": 1000},
                    }],
                },
                "tester",
            )
            job_id = batch["jobs"][0]["id"]
            wait_for_status(first, job_id, {"running"})
            first.close()

            second = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=adapter,
                vault=vault,
                start_scheduler=False,
            )
            try:
                recovered = second.get_job(job_id)
                self.assertEqual(recovered["status"], "running")
                self.assertTrue(recovered["remote_may_be_running"])
                self.assertFalse(recovered["cancel_requested"])
                self.assertTrue(any(
                    event["message"] == "Local monitor stopped for app shutdown; remote Kaggle run was preserved"
                    for event in recovered["events"]
                ))
            finally:
                second.close()

    def test_restart_recovers_legacy_restart_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "legacy-restart-source")
            database_path = root / "state.sqlite3"
            vault = EnvCredentialVault({"ACCOUNT_TOKEN": "token"})
            first = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                account = first.create_account(
                    account_payload("legacy-restart-user", "ACCOUNT_TOKEN"), "tester"
                )
                batch = first.create_batch(
                    {
                        "name": "legacy restart failure",
                        "jobs": [
                            {
                                "account_id": account["id"],
                                "experiment_name": "legacy restart failure",
                                "source_dir": str(source),
                                "kernel_slug": "legacy-restart-run",
                            }
                        ],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                with first.database.connection() as connection:
                    connection.execute(
                        "UPDATE jobs SET status='failed', remote_may_be_running=0, "
                        "error=? WHERE id=?",
                        (
                            "control plane restarted while this job was active; "
                            "verify Kaggle remotely",
                            job_id,
                        ),
                    )
            finally:
                first.close()

            second = ControlPlaneService(
                database_path,
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=vault,
                start_scheduler=False,
            )
            try:
                self.assertEqual(second.recovered_jobs, 1)
                recovered = second.get_job(job_id)
                self.assertEqual(recovered["status"], "running")
                self.assertTrue(recovered["remote_may_be_running"])
            finally:
                second.close()

    def test_disable_does_not_cancel_active_job_but_blocks_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "disable-source")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=EnvCredentialVault({"ACCOUNT_TOKEN": "token"}),
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            try:
                account = service.create_account(
                    account_payload("disable-user", "ACCOUNT_TOKEN"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "active before disable",
                        "jobs": [
                            {
                                "account_id": account["id"],
                                "experiment_name": "continues",
                                "source_dir": str(source),
                                "kernel_slug": "continues-after-disable",
                                "metadata": {"fake_polls": 30},
                            }
                        ],
                    },
                    "tester",
                )
                job_id = batch["jobs"][0]["id"]
                wait_for_status(service, job_id, {"running"})
                disabled = service.update_account(
                    account["id"], {"state": "disabled"}, "tester"
                )
                self.assertEqual(disabled["state"], "disabled")
                completed = wait_for_status(service, job_id, {"succeeded"})
                self.assertFalse(completed["cancel_requested"])
                self.assertFalse(completed["remote_may_be_running"])
                with self.assertRaisesRegex(ConflictError, "disabled"):
                    service.create_batch(
                        {
                            "name": "new blocked work",
                            "jobs": [
                                {
                                    "account_id": account["id"],
                                    "experiment_name": "blocked",
                                    "source_dir": str(source),
                                    "kernel_slug": "disabled-block",
                                }
                            ],
                        },
                        "tester",
                    )
            finally:
                service.close()

    def test_official_kaggle_quota_gates_accelerators_but_not_cpu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "quota-source")
            exhausted = {
                "source": "kaggle",
                "refresh_at": "2099-01-01T00:00:00",
                "resources": {
                    "gpu": {"used_hours": 30.0, "remaining_hours": 0.0, "total_hours": 30.0},
                    "tpu": {"used_hours": 20.0, "remaining_hours": 0.0, "total_hours": 20.0},
                },
            }
            adapter = FakeKaggleAdapter(poll_delay_seconds=0.005, quota_result=exhausted)
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=adapter,
                vault=EnvCredentialVault({"ACCOUNT_TOKEN": "token"}),
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("quota-user", "ACCOUNT_TOKEN"), "tester"
                )
                account = service.sync_account_quota(account["id"])["account"]
                self.assertEqual(
                    account["official_quota"]["gpu"]["remaining_hours"], 0
                )
                job_spec = {
                    "account_id": account["id"],
                    "experiment_name": "quota",
                    "source_dir": str(source),
                    "kernel_slug": "quota-run",
                    "metadata": {"accelerator": "gpu"},
                }
                with self.assertRaisesRegex(ConflictError, "official Kaggle GPU quota"):
                    service.create_batch(
                        {"name": "exhausted", "jobs": [job_spec]}, "tester"
                    )
                cpu_spec = {**job_spec, "kernel_slug": "cpu-still-allowed", "metadata": {"accelerator": "cpu"}}
                cpu_batch = service.create_batch(
                    {"name": "cpu allowed", "jobs": [cpu_spec]}, "tester"
                )
                adapter.quota_result = {
                    **exhausted,
                    "resources": {
                        **exhausted["resources"],
                        "gpu": {"used_hours": 0.0, "remaining_hours": 30.0, "total_hours": 30.0},
                    },
                }
                refreshed = service.sync_account_quota(account["id"])["account"]
                self.assertEqual(refreshed["official_quota"]["gpu"]["remaining_hours"], 30)
                gpu_batch = service.create_batch(
                    {"name": "gpu allowed after official refresh", "jobs": [job_spec]}, "tester"
                )
                service.scheduler.start()
                wait_for_status(service, cpu_batch["jobs"][0]["id"], {"succeeded"})
                wait_for_status(service, gpu_batch["jobs"][0]["id"], {"succeeded"})
            finally:
                service.close()

    def test_queued_cancel_is_terminal_without_remote_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "source")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(),
                vault=EnvCredentialVault({"ACCOUNT_TOKEN": "token"}),
                start_scheduler=False,
            )
            try:
                account = service.create_account(
                    account_payload("team-user", "ACCOUNT_TOKEN"), "tester"
                )
                batch = service.create_batch(
                    {
                        "name": "queued cancel",
                        "jobs": [
                            {
                                "account_id": account["id"],
                                "experiment_name": "queued",
                                "source_dir": str(source),
                                "kernel_slug": "queued-run",
                            }
                        ],
                    },
                    "tester",
                )
                cancelled = service.cancel_job(batch["jobs"][0]["id"], "tester")
                self.assertEqual(cancelled["cancel_semantics"], "queued_job_cancelled")
                self.assertEqual(cancelled["job"]["status"], "cancelled")
                self.assertFalse(cancelled["job"]["remote_may_be_running"])
            finally:
                service.close()


class ApiTests(unittest.TestCase):
    def test_health_auth_cors_and_fake_adapter_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_source(root, "api-source")
            service = ControlPlaneService(
                root / "state.sqlite3",
                data_dir=root / "data",
                allowed_source_root=root,
                adapter=FakeKaggleAdapter(poll_delay_seconds=0.005),
                vault=EnvCredentialVault({"API_ACCOUNT": "api-token"}),
                remote_poll_seconds=0.005,
                dispatch_poll_seconds=0.005,
            )
            server = create_server(service, port=0, api_token="control-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]

            def request(
                method: str,
                path: str,
                payload: dict[str, object] | None = None,
                *,
                authenticated: bool = True,
                origin: str = "http://localhost:3000",
                content_type: str = "application/json",
            ) -> tuple[int, dict[str, object], dict[str, str]]:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                headers = {
                    "Content-Type": content_type,
                    "Origin": origin,
                    "X-Actor": "api-test",
                }
                if authenticated:
                    headers["Authorization"] = "Bearer control-token"
                connection.request(
                    method,
                    path,
                    body=json.dumps(payload).encode() if payload is not None else None,
                    headers=headers,
                )
                response = connection.getresponse()
                raw = response.read()
                result = json.loads(raw) if raw else {}
                response_headers = dict(response.getheaders())
                connection.close()
                return response.status, result, response_headers

            try:
                status, health, headers = request("GET", "/api/health", authenticated=False)
                self.assertEqual(status, 200)
                self.assertEqual(health["status"], "ok")
                self.assertIn("version", health)
                self.assertIn("build_sha", health)
                self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:3000")
                status, _, _ = request("GET", "/api/accounts", authenticated=False)
                self.assertEqual(status, 401)
                status, forbidden, _ = request(
                    "GET", "/api/accounts", origin="https://attacker.example"
                )
                self.assertEqual(status, 403)
                self.assertEqual(forbidden["error"]["code"], "origin_forbidden")
                status, unsupported, _ = request(
                    "POST",
                    "/api/accounts",
                    account_payload("bad-content", "API_ACCOUNT"),
                    content_type="text/plain",
                )
                self.assertEqual(status, 415)
                self.assertEqual(
                    unsupported["error"]["code"], "unsupported_media_type"
                )

                status, created, _ = request(
                    "POST", "/api/accounts", account_payload("api-user", "API_ACCOUNT")
                )
                self.assertEqual(status, 201)
                account_id = created["account"]["id"]
                self.assertFalse(
                    created["account"]["remote_reconciliation_required"]
                )
                self.assertNotIn("used_hours_estimate", created["account"])
                status, reconciled, _ = request(
                    "POST",
                    f"/api/accounts/{account_id}/reconcile",
                    {"confirmed": True, "note": "No unresolved remote run"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(reconciled["reconciled_job_count"], 0)
                status, synced, _ = request(
                    "POST",
                    f"/api/accounts/{account_id}/quota/sync",
                    {},
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    synced["account"]["official_quota"]["gpu"]["remaining_hours"],
                    29.0,
                )
                status, queued, _ = request(
                    "POST",
                    "/api/batches",
                    {
                        "name": "API batch",
                        "jobs": [
                            {
                                "account_id": account_id,
                                "experiment_name": "api experiment",
                                "source_dir": str(source),
                                "kernel_slug": "api-experiment",
                                "metadata": {"fake_result": {"metric": 7}},
                            }
                        ],
                    },
                )
                self.assertEqual(status, 202)
                job_id = queued["batch"]["jobs"][0]["id"]
                deadline = time.monotonic() + 5
                result: dict[str, object] = {}
                while time.monotonic() < deadline:
                    status, result, _ = request("GET", f"/api/jobs/{job_id}/result")
                    if result.get("ready"):
                        break
                    time.sleep(0.01)
                self.assertEqual(status, 200)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(
                    result["result"]["output"]["fake_result"]["metric"], 7
                )
                status, summaries, _ = request(
                    "GET", "/api/jobs?status=succeeded&limit=1&summary=1"
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(summaries["jobs"]), 1)
                self.assertEqual(summaries["jobs"][0]["id"], job_id)
                self.assertNotIn("result", summaries["jobs"][0])
                status, stats, _ = request("GET", "/api/jobs/stats")
                self.assertEqual(status, 200)
                self.assertEqual(stats["states"], {"succeeded": 1})
                status, compact, _ = request(
                    "GET",
                    f"/api/jobs/{job_id}?include_remote_logs=0&event_limit=1",
                )
                self.assertEqual(status, 200)
                self.assertLessEqual(len(compact["job"]["events"]), 1)
                self.assertNotIn("remote_logs", compact["job"])
                status, invalid_limit, _ = request("GET", "/api/jobs?limit=0")
                self.assertEqual(status, 422)
                self.assertEqual(
                    invalid_limit["error"]["message"],
                    "job limit must be between 1 and 1000",
                )
                status, audit, _ = request("GET", "/api/audit?limit=20")
                self.assertEqual(status, 200)
                self.assertTrue(audit["audit"])
                client_events = [
                    event
                    for event in audit["audit"]
                    if event["action"].startswith("account.")
                    or event["action"] == "batch.created"
                ]
                self.assertTrue(client_events)
                self.assertTrue(
                    all(
                        event["actor"] in {"authenticated-client", "quota-sync"}
                        for event in client_events
                    )
                )
                self.assertNotIn("api-test", {event["actor"] for event in audit["audit"]})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                service.close()


if __name__ == "__main__":
    unittest.main()

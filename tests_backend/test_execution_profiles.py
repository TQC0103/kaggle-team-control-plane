"""Offline execution contracts: no remote accounts or quota are used here."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from control_plane.errors import ValidationError
from control_plane.execution_profiles import validate_profile
from control_plane.offline_runtime import run
from control_plane.competition_access import entered_competitions
from control_plane.adapters import stage_job_source, AdapterError

ROOT = Path(__file__).resolve().parents[1]


class ExecutionProfileTests(unittest.TestCase):
    def bundle(self, root, shape='CPU', competition=None):
        work = root / 'work.py'
        work.write_text("print('offline')\n")
        source = root / 'bundle'
        cmd = [sys.executable, str(ROOT/'scripts/package-offline-job.py'),
               '--source', str(source), '--workload', str(work), '--owner', 'owner',
               '--slug', 'offline-test', '--shape', shape]
        if competition:
            cmd += ['--competition', competition, '--rules-url',
                    'https://www.kaggle.com/competitions/'+competition+'/rules']
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return source

    def test_standard_legacy_unchanged_advanced_requires_profile(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(validate_profile(d, 'NvidiaTeslaT4'))
            with self.assertRaises(ValidationError):
                validate_profile(d, 'NvidiaL4')

    def test_generated_advanced_bundle_checks_quota_attachment_and_network(self):
        with tempfile.TemporaryDirectory() as d:
            source = self.bundle(Path(d), 'NvidiaL4', 'example-competition')
            self.assertEqual(validate_profile(source, 'NvidiaL4', 1)['internet'], False)
            with self.assertRaisesRegex(ValidationError, 'quota'):
                validate_profile(source, 'NvidiaL4', 0.01)
            meta_path = source/'kernel-metadata.json'
            original = json.loads(meta_path.read_text())
            for key, value in [('enable_internet', True), ('competition_sources', [])]:
                meta_path.write_text(json.dumps({**original, key: value}))
                with self.assertRaises(ValidationError):
                    validate_profile(source, 'NvidiaL4')

    def test_entrypoint_tamper_and_runtime_spec_drift_fail(self):
        with tempfile.TemporaryDirectory() as d:
            source = self.bundle(Path(d))
            entry = source/'run.py'
            entry.write_text(entry.read_text()+"\nprint('changed')\n")
            with self.assertRaisesRegex(ValidationError, 'hash'):
                validate_profile(source, 'CPU')
            profile_path = source/'offline-profile.json'
            p = json.loads(profile_path.read_text())
            p['entrypoint_sha256'] = hashlib.sha256(entry.read_bytes()).hexdigest()
            p['runtime']['python_abi'] = 'cp999'
            profile_path.write_text(json.dumps(p))
            with self.assertRaisesRegex(ValidationError, 'drift'):
                validate_profile(source, 'CPU')

    def test_staging_preserves_offline_and_rejects_changed_profile(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.bundle(root, 'NvidiaL4', 'example-competition')
            job = dict(id='job_offline', source_dir=str(source), kernel_slug='offline-test',
                       metadata=dict(accelerator='gpu', machine_shape='NvidiaL4',
                                     offline_preflight=validate_profile(source, 'NvidiaL4')))
            account = {'kaggle_username': 'owner'}
            stage_job_source(job, account, root/'staged')
            meta = json.loads((root/'staged/job_offline/kernel-metadata.json').read_text())
            self.assertIs(meta['enable_internet'], False)
            self.assertEqual(meta['machine_shape'], 'NvidiaL4')
            p = source/'offline-profile.json'; data = json.loads(p.read_text())
            data['estimated_session_minutes'] = 6; p.write_text(json.dumps(data))
            with self.assertRaisesRegex(AdapterError, 'changed'):
                stage_job_source(job, account, root/'staged')

    def test_abi_mismatch_stops_before_venv(self):
        with tempfile.TemporaryDirectory() as d, patch('control_plane.offline_runtime.Path.cwd', return_value=Path(d)):
            with self.assertRaisesRegex(RuntimeError, 'ABI mismatch'):
                run({'python_abi': 'cp999'}, '')
            self.assertFalse((Path(d)/'kcp-offline/venv').exists())

    def test_native_job_executes_in_venv_and_does_not_change_parent_env(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.bundle(root)
            env = os.environ.copy()
            env['PYTHONPATH'] = str(root/'deliberately-absent')
            subprocess.run([sys.executable, str(source/'run.py')], cwd=root, env=env,
                           check=True, capture_output=True, text=True, timeout=90)
            runtime = json.loads((root/'kcp-offline/runtime.json').read_text())
            self.assertEqual(runtime['strategy'], 'native')
            self.assertFalse((root/'kcp-offline/venv').exists())
            self.assertEqual(env['PYTHONPATH'], str(root/'deliberately-absent'))

    def test_hashed_universal_wheel_installs_without_index(self):
        # A self-built fixture proves installation without downloading any package.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); wheel_dir = root/'input'; wheel_dir.mkdir()
            wheel = wheel_dir/'kcp_fixture-1.0-py3-none-any.whl'
            with zipfile.ZipFile(wheel, 'w') as z:
                z.writestr('kcp_fixture.py', 'VALUE = 17\n')
                z.writestr('kcp_fixture-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: kcp-fixture\nVersion: 1.0\n')
                z.writestr('kcp_fixture-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: kcp-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                z.writestr('kcp_fixture-1.0.dist-info/RECORD', '')
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            lock = wheel_dir/'requirements.lock'
            lock.write_text('kcp-fixture==1.0 --hash=sha256:'+digest+'\n')
            files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [wheel, lock]}
            manifest = wheel_dir/'fixture-manifest.json'
            manifest.write_text(json.dumps({'files': files}))
            workload = "import kcp_fixture\nassert kcp_fixture.VALUE == 17\n"
            spec = dict(strategy='wheelhouse', python_abi='cp'+str(sys.version_info.major)+str(sys.version_info.minor),
                        input_root=str(wheel_dir), wheelhouse_manifest=manifest.name,
                        wheelhouse_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                        expected_packages={'kcp-fixture': '1.0'},
                        workload_sha256=hashlib.sha256(workload.encode()).hexdigest())
            with patch('control_plane.offline_runtime.Path.cwd', return_value=root):
                run(spec, workload)
            self.assertFalse((root/'kcp-offline/venv').exists())

    def test_membership_discovery_is_read_only_and_does_not_claim_entitlement(self):
        with patch('control_plane.competition_access._account_cli') as cli:
            cli.return_value = ({}, lambda args, timeout: 'ref,deadline,title\nhttps://www.kaggle.com/competitions/example-comp,2026-12-01,Example\n')
            result = entered_competitions(object(), 'account')
        self.assertTrue(result['membership_only'])
        self.assertEqual(result['advanced_access'], 'unverified')
        self.assertEqual(result['competitions'][0]['slug'], 'example-comp')


if __name__ == '__main__':
    unittest.main()

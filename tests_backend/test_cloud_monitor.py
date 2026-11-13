from contextlib import contextmanager
import hashlib
import io
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock
import unittest

from cloud_orchestrator.connection import PROJECT_NUMBER
from cloud_orchestrator.kaggle_monitor import (
    GoogleKaggleSessions, KaggleObserver, MonitorMismatch, probe,
)
from cloud_orchestrator.storage import MemoryStore
from cloud_orchestrator.service import tick

ACCOUNT = 'acct_'+'1'*32
SOURCE = 'scientific source\n'
ROW = dict(id='job_fixture', account_id=ACCOUNT, kernel_slug='fixture/experiment-v1',
           version=1, source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest())
ITEM = dict(id=ACCOUNT, username='fixture')


class MonitorTests(unittest.TestCase):
    def client(self, **override):
        meta = NS(id=100, is_private=True, author='Human display name', ref='fixture/experiment-v1', slug='experiment-v1',
                  current_version_number=1, machine_shape='NvidiaTeslaT4', **override)
        api = Mock()
        api.get_kernel.return_value = NS(metadata=meta, blob=NS(source=SOURCE))
        api.get_kernel_session_status.return_value = NS(status=NS(name='RUNNING'))
        return NS(kernels=NS(kernels_api_client=api)), api

    def test_status_is_fenced_with_version_and_source_before_and_after(self):
        client, api = self.client()
        value = probe(client, ITEM, ROW, clock=lambda: 1)
        self.assertEqual(api.get_kernel.call_count, 2)
        self.assertEqual(api.get_kernel.call_args.args[0].kernel_slug, 'experiment-v1')
        self.assertEqual(api.get_kernel_session_status.call_args.args[0].version_label, '')
        self.assertEqual(value['ref'], 'fixture/experiment-v1/1')
        self.assertEqual(value['status'], 'running')
        self.assertEqual(value['source_sha256'], ROW['source_sha256'])

    def test_source_drift_blocks_status_query(self):
        client, api = self.client()
        api.get_kernel.return_value.blob.source = 'changed'
        with self.assertRaisesRegex(MonitorMismatch, 'SOURCE_SHA_MISMATCH'):
            probe(client, ITEM, ROW)
        api.get_kernel_session_status.assert_not_called()

    def test_version_drift_blocks_status_query(self):
        client, api = self.client()
        api.get_kernel.return_value.metadata.current_version_number = 2
        with self.assertRaises(MonitorMismatch): probe(client, ITEM, ROW)
        api.get_kernel_session_status.assert_not_called()

    def test_version_change_during_status_is_not_reported_as_exact(self):
        client, api = self.client()
        before = api.get_kernel.return_value
        after = NS(metadata=NS(**dict(vars(before.metadata), current_version_number=2)), blob=before.blob)
        api.get_kernel.side_effect = [before, after]
        with self.assertRaisesRegex(MonitorMismatch, 'CHANGED_DURING_STATUS'):
            probe(client, ITEM, ROW)

    def test_recreated_notebook_is_not_accepted(self):
        client, api = self.client()
        with self.assertRaisesRegex(MonitorMismatch, 'KERNEL_ID_CHANGED'):
            probe(client, ITEM, dict(ROW, kernel_id=999))

    def test_canonical_ref_not_human_display_name_binds_owner(self):
        client, api = self.client()
        api.get_kernel.return_value.metadata.ref = 'another/experiment-v1'
        with self.assertRaisesRegex(MonitorMismatch, 'KERNEL_IDENTITY_MISMATCH'):
            probe(client, ITEM, ROW)
        api.get_kernel_session_status.assert_not_called()

    def test_cross_account_never_makes_rpc(self):
        client, api = self.client()
        with self.assertRaises(MonitorMismatch): probe(client, dict(ITEM, username='another'), ROW)
        api.get_kernel.assert_not_called()

    def test_public_kernel_not_accepted(self):
        client, api = self.client()
        api.get_kernel.return_value.metadata.is_private = False
        with self.assertRaises(MonitorMismatch): probe(client, ITEM, ROW)

    def test_unknown_status_is_never_assumed_complete(self):
        client, api = self.client()
        api.get_kernel_session_status.return_value.status.name = 'UNEXPECTED'
        self.assertEqual(probe(client, ITEM, ROW)['status'], 'unknown')

    def test_sdk_error_message_never_returned(self):
        class Sessions:
            def load(self): return {ACCOUNT: ITEM}
            @contextmanager
            def authenticated(self, _):
                raise RuntimeError('SENSITIVE_TOKEN_FROM_SDK')
                yield
        result = KaggleObserver(Sessions()).observe([ROW], deadline=10, clock=lambda: 1)
        self.assertNotIn('SENSITIVE_TOKEN_FROM_SDK', str(result))
        self.assertEqual(result['job_fixture']['error_class'], 'RuntimeError')

    def test_deadline_prevents_new_rpc(self):
        class Sessions:
            def load(self): return {}
            def authenticated(self, _): raise AssertionError('deadline must prevent RPC')
        self.assertEqual(KaggleObserver(Sessions()).observe([ROW], deadline=0, clock=lambda: 1), {})

    def test_vault_project_or_plan_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            GoogleKaggleSessions([dict(version_name='projects/foreign/secrets/kcp-kaggle-vault-aaaaaaaaaaaa-g01/versions/1',
                                      plan_sha256='a'*64)], secret_client=Mock())

    def test_vault_read_pins_numeric_project_version_and_never_echoes_payload(self):
        version = f'projects/{PROJECT_NUMBER}/secrets/kcp-kaggle-vault-aaaaaaaaaaaa-g01/versions/1'
        config = [dict(version_name=version, plan_sha256='b'*64)]
        client = Mock()
        client.access_secret_version.return_value = NS(payload=NS(data=json.dumps(dict(schema=1,
            plan_sha256='b'*64, accounts=[dict(ITEM, kind='kaggle_static_v1', auth=dict(token='SECRET'))])).encode()))
        sessions = GoogleKaggleSessions(config, secret_client=client)
        self.assertEqual(sessions.load()[ACCOUNT]['username'], 'fixture')
        self.assertEqual(client.access_secret_version.call_args.kwargs['request']['name'], version)

    def test_real_observer_results_share_the_fenced_tick_without_submission(self):
        store = MemoryStore()
        store.monitors = [ROW]
        observer = Mock()
        observer.observe.return_value = {'job_fixture': {'status': 'running', 'observed_at': 1000}}
        result = tick(store, clock=lambda: 1000, observer=observer)
        self.assertEqual(result['monitored'], 1)
        self.assertFalse(result['submission_enabled'])
        self.assertEqual(store.monitor_results['job_fixture']['status'], 'running')

    def test_lost_tick_fence_cannot_publish_observations(self):
        store = MemoryStore()
        store.monitors = [ROW]
        observer = Mock()
        def late(*_args, **_kw):
            store.control['fence'] += 1
            return {'job_fixture': {'status': 'complete'}}
        observer.observe.side_effect = late
        result = tick(store, clock=lambda: 1000, observer=observer)
        self.assertEqual(result['status'], 'LOST_LEASE')
        self.assertEqual(store.monitor_results, {})


if __name__ == '__main__':
    unittest.main()

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from control_plane.adapters import FakeKaggleAdapter, KaggleCliAdapter, RemoteStatus
from control_plane.credentials import EnvCredentialVault
from control_plane.errors import ValidationError
from control_plane.remote_cancel import cancellation_identity, cancel_session
from control_plane.service import ControlPlaneService
from tests_backend.test_control_plane import account_payload, make_source


class CancellationIdentityTests(unittest.TestCase):
    job = {'kernel_slug': 'owner/experiment', 'metadata': {}}

    def test_no_guess_from_notebook_or_version(self):
        job = dict(self.job, metadata={'kernel_id': 123456, 'version_number': 1})
        self.assertIsNone(cancellation_identity(job, {}))
        result = cancel_session(job, {}, {})
        self.assertFalse(result['request_sent'])
        self.assertEqual(result['semantics'], 'remote_cancellation_requires_session_id')

    def test_invalid_ids_and_mismatched_ref_rejected(self):
        for value in (True, -1, 0, 1.2, '123'):
            with self.assertRaises(ValidationError):
                cancellation_identity(self.job, {'kernel_session_id': value, 'session_ref': 'owner/experiment'})
        with self.assertRaises(ValidationError):
            cancellation_identity(self.job, {'kernel_session_id': 999, 'session_ref': 'owner/another'})

    def test_sdk_cancel_once_with_exact_id(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.kernels.kernels_api_client.cancel_kernel_session.return_value = SimpleNamespace(error_message='')
        with patch('kagglesdk.kaggle_client.KaggleClient', return_value=client):
            result = cancel_session(self.job, {'KAGGLE_USERNAME': 'owner', 'KAGGLE_API_TOKEN': 'synthetic-token'},
                {'kernel_session_id': 999, 'session_ref': 'owner/experiment'})
        request = client.kernels.kernels_api_client.cancel_kernel_session.call_args.args[0]
        self.assertEqual(request.kernel_session_id, 999)
        self.assertEqual(client.kernels.kernels_api_client.cancel_kernel_session.call_count, 1)
        self.assertTrue(result['request_sent'])
        self.assertNotIn('synthetic-token', str(result))

    def test_wrong_assigned_owner_rejected_before_sdk(self):
        with self.assertRaises(ValidationError):
            cancel_session(self.job, {'KAGGLE_USERNAME': 'other'},
                {'kernel_session_id': 999, 'session_ref': 'owner/experiment'})

    def test_only_explicit_cancellation_error_counts_as_cancelled(self):
        adapter = KaggleCliAdapter()
        job = dict(self.job, cancel_requested=True)
        with patch.object(adapter, '_run', return_value='owner/experiment has status "KernelWorkerStatus.ERROR"\nFailure message: "Cancelled by user"'):
            self.assertEqual(adapter.status(job, {}, threading.Event()).state, 'cancelled')
        with patch.object(adapter, '_run', return_value='owner/experiment has status "KernelWorkerStatus.ERROR"\nFailure message: "out of memory"'):
            self.assertEqual(adapter.status(job, {}, threading.Event()).state, 'failed')


class ControlledAdapter(FakeKaggleAdapter):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.state = 'queued'
        self.raise_cancel = False

    def cancel(self, job, env, payload):
        session_id = cancellation_identity(job, payload)
        if session_id is None:
            return {'request_sent': False, 'semantics': 'remote_cancellation_requires_session_id'}
        self.calls += 1
        if self.raise_cancel:
            raise TimeoutError('response lost after sending cancellation')
        return {'request_sent': True, 'semantics': 'remote_cancellation_pending'}

    def status(self, job, env, cancel_event):
        return RemoteStatus(self.state, 'controlled remote ' + self.state)


class RemoteCancellationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.adapter = ControlledAdapter()
        self.service = ControlPlaneService(root/'state.sqlite3', data_dir=root/'data',
            allowed_source_root=root, adapter=self.adapter,
            vault=EnvCredentialVault({'TEST_AUTH': 'synthetic-token'}), start_scheduler=False)
        self.rechecks = patch.object(self.service, '_schedule_recovered_job_recheck')
        self.rechecks.start()
        account = self.service.create_account(account_payload('owner', 'TEST_AUTH'), 'test')
        batch = self.service.create_batch({'name': 'cancel', 'jobs': [{'account_id': account['id'],
            'experiment_name': 'cancel', 'source_dir': str(make_source(root, 'source')), 'kernel_slug': 'experiment'}]}, 'test')
        self.id = batch['jobs'][0]['id']
        self.ref = 'owner/experiment'
        self.payload = {'kernel_session_id': 999, 'session_ref': self.ref}

    def tearDown(self):
        self.rechecks.stop()
        self.service.close()
        self.temp.cleanup()

    def submitted(self):
        self.service.database.transition_job(self.id, {'queued'}, 'submitted', fields={'result': {'submit': {'kernel_slug': self.ref}}})

    def test_local_queue_cancel_does_not_call_remote(self):
        result = self.service.cancel_job(self.id, 'test')
        self.assertEqual(result['cancel_semantics'], 'queued_job_cancelled')
        self.assertEqual(result['job']['status'], 'cancelled')
        self.assertEqual(self.adapter.calls, 0)

    def test_missing_id_does_not_mark_remote_cancelled(self):
        self.submitted()
        result = self.service.cancel_job(self.id, 'test')
        self.assertEqual(result['cancel_semantics'], 'remote_cancellation_requires_session_id')
        self.assertEqual(result['job']['status'], 'cancel_requested')
        self.assertTrue(result['job']['remote_may_be_running'])
        self.assertEqual(self.adapter.calls, 0)

    def test_accepted_request_is_not_confirmation_and_not_duplicated(self):
        self.submitted()
        result = self.service.cancel_job(self.id, 'test', self.payload)
        self.assertEqual(result['job']['status'], 'cancel_requested')
        self.service.cancel_job(self.id, 'test', self.payload)
        self.assertEqual(self.adapter.calls, 1)

    def test_verified_cancel_releases_remote_uncertainty(self):
        self.submitted(); self.adapter.state = 'cancelled'
        result = self.service.cancel_job(self.id, 'test', self.payload)
        self.assertEqual(result['cancel_semantics'], 'remote_cancellation_confirmed')
        self.assertEqual(result['job']['status'], 'cancelled')
        self.assertFalse(result['job']['remote_may_be_running'])

    def test_ambiguous_mutation_timeout_not_replayed(self):
        self.submitted(); self.adapter.raise_cancel = True
        result = self.service.cancel_job(self.id, 'test', self.payload)
        self.assertEqual(result['cancel_semantics'], 'remote_cancellation_unconfirmed')
        self.service.cancel_job(self.id, 'test', self.payload)
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.service.get_job(self.id)['status'], 'cancel_requested')

    def test_recovery_preserves_cancel_intent_when_still_queued(self):
        self.submitted()
        self.service.cancel_job(self.id, 'test', self.payload)
        self.service._reconcile_recovered_jobs([self.id])
        result = self.service.get_job(self.id)
        self.assertEqual(result['status'], 'cancel_requested')
        self.assertTrue(result['remote_may_be_running'])
        self.assertEqual(self.adapter.calls, 1)

    def test_invalid_payload_does_not_change_job_state(self):
        self.submitted()
        with self.assertRaises(ValidationError):
            self.service.cancel_job(self.id, 'test', {'kernel_session_id': 999, 'session_ref': 'other/job'})
        self.assertEqual(self.service.get_job(self.id)['status'], 'submitted')

    def test_late_remote_confirmation_preserves_identity_and_semantics(self):
        self.submitted()
        self.service.cancel_job(self.id, 'test', self.payload)
        self.adapter.state = 'cancelled'
        self.service._reconcile_recovered_jobs([self.id])
        job = self.service.get_job(self.id)
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(job['cancel_semantics'], 'remote_cancellation_confirmed')
        self.assertEqual(job['result']['cancellation']['kernel_session_id'], 999)


if __name__ == '__main__': unittest.main()

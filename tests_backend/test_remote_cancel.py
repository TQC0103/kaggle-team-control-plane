import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from control_plane.adapters import FakeKaggleAdapter, KaggleCliAdapter, RemoteStatus
from control_plane.credentials import EnvCredentialVault
from control_plane.errors import ValidationError
from control_plane.remote_cancel import cancellation_identity, cancel_session, submitted_version, parse_session_redirect, resolve_session_identity
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

    def test_submission_version_is_bound_to_original_receipt(self):
        self.assertEqual(submitted_version({'result':{'submit':{'cli_message':'Kernel version 7 successfully pushed.'}}}),7)
        with self.assertRaises(ValidationError):submitted_version({'metadata':{'version_number':7}})

    def test_redirect_parser_rejects_non_session_urls(self):
        self.assertEqual(parse_session_redirect('https://api.kaggle.com/v1/kernels/output/download_zip/999'),999)
        for url in ('http://api.kaggle.com/v1/kernels/output/download_zip/999',
                    'https://evil.example/v1/kernels/output/download_zip/999',
                    'https://api.kaggle.com/notebook/999',
                    'https://api.kaggle.com:444/v1/kernels/output/download_zip/999',
                    'https://user@api.kaggle.com/v1/kernels/output/download_zip/999'):
            with self.assertRaises(ValidationError):parse_session_redirect(url)

    def resolver_client(self, ref='owner/experiment', version=7):
        client=MagicMock();client.__enter__.return_value=client
        client.kernels.kernels_api_client.get_kernel.return_value=SimpleNamespace(metadata=SimpleNamespace(ref=ref,current_version_number=version,id=123))
        response=MagicMock(status_code=302,headers={'Location':'https://api.kaggle.com/v1/kernels/output/download_zip/999'})
        client.http_client()._session.send.return_value=response
        return client,response

    def test_resolves_server_session_id_without_downloading_zip(self):
        client,response=self.resolver_client()
        job=dict(self.job,result={'submit':{'version_number':7}})
        with patch('kagglesdk.kaggle_client.KaggleClient',return_value=client):
            result=resolve_session_identity(job,{'KAGGLE_USERNAME':'owner','KAGGLE_API_TOKEN':'synthetic-token'})
        self.assertEqual(result['kernel_session_id'],999)
        self.assertEqual(result['notebook_id'],123)
        self.assertEqual(result['version_number'],7)
        self.assertFalse(result['remote_mutation'])
        kwargs=client.http_client()._session.send.keywords
        self.assertEqual(kwargs['timeout'],20)
        send_args=client.http_client()._session.send.func.call_args.kwargs
        self.assertFalse(send_args['allow_redirects'])
        self.assertTrue(send_args['stream'])
        response.close.assert_called_once()
        client.kernels.kernels_api_client.cancel_kernel_session.assert_not_called()

    def test_resolver_rejects_newer_version_or_wrong_notebook(self):
        for ref,version in (('owner/experiment',8),('owner/different',7)):
            client,_=self.resolver_client(ref,version)
            with patch('kagglesdk.kaggle_client.KaggleClient',return_value=client):
                with self.assertRaises(ValidationError):resolve_session_identity(dict(self.job,result={'submit':{'version_number':7}}),{'KAGGLE_USERNAME':'owner','KAGGLE_API_TOKEN':'synthetic-token'})
            client.http_client()._prepare_request.assert_not_called()
            client.kernels.kernels_api_client.cancel_kernel_session.assert_not_called()

    def test_status_verification_is_pinned_to_resolved_version(self):
        adapter=KaggleCliAdapter()
        job=dict(self.job,cancel_requested=True,result={'cancellation':{'resolution':{'version_number':7}}})
        with patch.object(adapter,'_run',return_value='owner/experiment/7 has status "KernelWorkerStatus.CANCEL_ACKNOWLEDGED"') as run:
            self.assertEqual(adapter.status(job,{},threading.Event()).state,'cancelled')
        self.assertEqual(run.call_args.args[0],['kernels','status','owner/experiment/7'])


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

    def test_auto_identity_persisted_before_single_mutation(self):
        self.submitted()
        def resolve(job,env):return dict(self.payload,version_number=1,method='official_output_download_redirect')
        self.adapter.resolve_cancel_identity=resolve
        original=self.adapter.cancel
        def cancel(job,env,payload):
            stored=self.service.get_job(self.id)['result']['cancellation']
            self.assertTrue(stored['request_started'])
            self.assertEqual(stored['resolution']['kernel_session_id'],999)
            return original(job,env,payload)
        self.adapter.cancel=cancel
        result=self.service.cancel_job(self.id,'test')
        self.assertEqual(result['cancel_semantics'],'remote_cancellation_pending')
        self.assertEqual(self.adapter.calls,1)

    def test_read_only_resolution_failure_does_not_send_cancel(self):
        self.submitted()
        def resolve(job,env):raise TimeoutError('read-only resolution timed out')
        self.adapter.resolve_cancel_identity=resolve
        result=self.service.cancel_job(self.id,'test')
        self.assertEqual(result['job']['status'],'cancel_requested')
        self.assertEqual(self.adapter.calls,0)
        self.assertFalse(result['job']['result']['cancellation']['request_started'])


if __name__ == '__main__': unittest.main()

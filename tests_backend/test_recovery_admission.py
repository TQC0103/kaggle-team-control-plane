"""Restart admission must count verified remote sessions without blocking spare slots."""
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError,URLError

from control_plane.adapters import FakeKaggleAdapter,KaggleCliAdapter,RemoteStatus
from control_plane.credentials import EnvCredentialVault
from control_plane.errors import ConflictError
from control_plane.service import ControlPlaneService
from tests_backend.test_control_plane import make_source,account_payload,wait_for_status

class ActiveAdapter(FakeKaggleAdapter):
    fail=False
    state='running'
    submits=0
    def status(self,job,env,cancel_event):
        if self.fail:raise RuntimeError('temporary status timeout')
        return RemoteStatus(self.state,'exact remote status')
    def submit(self,job,env,cancel_event):
        self.submits+=1
        return super().submit(job,env,cancel_event)

class RecoveryAdmissionTests(unittest.TestCase):
    def fixture(self,root,adapter):
        service=ControlPlaneService(root/'state.sqlite3',data_dir=root/'data',allowed_source_root=root,adapter=adapter,vault=EnvCredentialVault({'ACCOUNT_TOKEN':'fake'}),start_scheduler=False,max_jobs_per_account=2,remote_poll_seconds=.005)
        account=service.create_account(account_payload('research-owner','ACCOUNT_TOKEN'),'test')
        source=make_source(root,'original')
        job=service.create_batch(dict(name='first',jobs=[dict(account_id=account['id'],experiment_name='original',source_dir=str(source),kernel_slug='original')]),'test')['jobs'][0]
        receipt=dict(kernel_slug=job['kernel_slug'],version_number=1,remote_verification=dict(state='running'))
        service.database.transition_job(job['id'],{'queued'},'running',fields=dict(result={'submit':receipt}))
        service.close()
        recovered=ControlPlaneService(root/'state.sqlite3',data_dir=root/'data',allowed_source_root=root,adapter=adapter,vault=EnvCredentialVault({'ACCOUNT_TOKEN':'fake'}),start_scheduler=False,max_jobs_per_account=2,remote_poll_seconds=.005)
        self.addCleanup(recovered.close)
        # The fake deterministic service does not need real polling threads.
        recovered._schedule_recovered_job_recheck=lambda _:None
        return recovered,account,job,receipt

    def test_verified_running_reserves_one_slot_and_allows_only_one_more(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);adapter=ActiveAdapter();service,account,job,receipt=self.fixture(root,adapter)
            self.assertFalse(service.get_account(account['id'])['remote_reconciliation_required'])
            self.assertEqual(service.get_account(account['id'])['active_runs'],1)
            self.assertEqual(service.get_job(job['id'])['result']['submit'],receipt)
            source=make_source(root,'new')
            jobs=service.create_batch(dict(name='next',jobs=[dict(account_id=account['id'],experiment_name=str(i),source_dir=str(source),kernel_slug='next-'+str(i)) for i in range(2)]),'test')['jobs']
            service.scheduler._dispatch_available()
            active=[j for j in jobs if service.get_job(j['id'])['status']!='queued'];self.assertEqual(len(active),1)
            wait_for_status(service,active[0]['id'],{'running'})
            service.scheduler._dispatch_available()
            self.assertEqual(service.get_account(account['id'])['active_runs'],2)
            self.assertEqual(sum(service.get_job(j['id'])['status']=='queued' for j in jobs),1)
            self.assertEqual(adapter.submits,1) # Never replay original push.
            self.assertEqual(service.get_job(job['id'])['status'],'running')
            service.close() # Join the worker before its temporary DB is removed.

    def test_verified_queued_session_also_reserves_slot(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=ActiveAdapter();adapter.state='queued';s,a,j,_=self.fixture(Path(d),adapter)
            self.assertFalse(s.get_account(a['id'])['remote_reconciliation_required'])
            self.assertEqual(s.get_account(a['id'])['active_runs'],1)
            self.assertEqual(s.get_job(j['id'])['status'],'submitted')

    def test_status_failure_reinstates_guard_without_releasing_session(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);adapter=ActiveAdapter();s,a,j,_=self.fixture(root,adapter);adapter.fail=True
            s._reconcile_recovered_jobs([j['id']],announce_restart=False)
            self.assertTrue(s.get_account(a['id'])['remote_reconciliation_required'])
            self.assertEqual(s.get_account(a['id'])['active_runs'],1)
            with self.assertRaisesRegex(ConflictError,'reconciliation'):
                s.create_batch(dict(name='blocked',jobs=[dict(account_id=a['id'],experiment_name='blocked',source_dir=str(make_source(root,'blocked')),kernel_slug='blocked')]),'test')
            adapter.fail=False;s._reconcile_recovered_jobs([j['id']],announce_restart=False)
            self.assertFalse(s.get_account(a['id'])['remote_reconciliation_required'])
            self.assertEqual(s.get_account(a['id'])['active_runs'],1)

    def test_complete_preserves_original_submission_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=ActiveAdapter();s,a,j,receipt=self.fixture(Path(d),adapter);adapter.state='complete'
            s._reconcile_recovered_jobs([j['id']],announce_restart=False)
            result=s.get_job(j['id'])['result'];self.assertEqual(result['submit'],receipt)
            self.assertFalse(result['output_pending']);self.assertEqual(s.get_account(a['id'])['active_runs'],0)

    def test_status_query_pins_original_version(self):
        adapter=KaggleCliAdapter()
        job=dict(kernel_slug='owner/slug',result={'submit':dict(kernel_slug='owner/slug',version_number=3)})
        with patch.object(adapter,'_run',return_value='owner/slug has status "KernelWorkerStatus.RUNNING"') as run:
            adapter.status(job,{},threading.Event())
        self.assertEqual(run.call_args.args[0],['kernels','status','owner/slug/3'])

class BridgeErrorTests(unittest.TestCase):
    def test_http_conflict_is_not_reported_as_offline(self):
        path=Path(__file__).resolve().parents[1]/'plugins/kaggle-control-plane/scripts/server.py'
        spec=importlib.util.spec_from_file_location('kcp_test_bridge',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        error=HTTPError('http://127.0.0.1:8765',409,'Conflict',{},io.BytesIO(json.dumps({'error':{'message':'account requires remote reconciliation'}}).encode()))
        with patch.object(module.urllib.request,'urlopen',side_effect=error):
            with self.assertRaisesRegex(RuntimeError,'HTTP 409: account requires remote reconciliation'):module._request('/api/batches')
        with patch.object(module.urllib.request,'urlopen',side_effect=URLError('connection refused')):
            with self.assertRaisesRegex(RuntimeError,'API is offline'):module._request('/api/health')

if __name__=='__main__':unittest.main()

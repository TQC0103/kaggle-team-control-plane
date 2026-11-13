import csv,io,json,tempfile,time,unittest,threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from control_plane.artifact_archive import ArtifactArchiver,digest
from control_plane.datasets import DatasetManager,_now
from control_plane.errors import ValidationError
from control_plane.service import ControlPlaneService
from control_plane.dataset_transfer import _account_cli
from control_plane.adapters import KaggleCliAdapter
from control_plane.dataset_library import DatasetLibrary

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.a={'id':'account','state':'enabled','kaggle_username':'owner','credential_env_ref':'TEST'}
        self.jobs=[];self.proofs=[];self.remote={};self.creates=0;self.downloads=[];self.forbidden=False;self.lost_reply=False;self.fail_download=0
        db=SimpleNamespace(list_jobs=lambda:self.jobs,list_accounts=lambda:[self.a],get_account=lambda aid:self.a,get_job=lambda jid,**kw:next(j for j in self.jobs if j['id']==jid),append_audit=lambda *args:None)
        self.service=SimpleNamespace(data_dir=self.root/'internal',allowed_source_root=self.root/'experiments',artifact_root=self.root/'internal/artifacts',database=db,_reject_secrets=lambda p:None)
        for p in [self.service.data_dir,self.service.allowed_source_root,self.service.artifact_root]:p.mkdir(parents=True,exist_ok=True)
        self.service.dataset_library=DatasetLibrary(self.service,lambda aid:(self.a,self.cli))
        real_register=self.service.dataset_library.register_verification
        def register(payload,actor):
            result=real_register(payload,actor);self.proofs.append(payload);return result
        self.service.dataset_library.register_verification=register
        self.service.datasets=DatasetManager(self.service,lambda aid:(self.a,self.cli))
        self.archive=ArtifactArchiver(self.service,lambda aid:(self.a,self.cli));self.archive.configure({'enabled':True})
    def tearDown(self):
        self.archive.close();self.service.datasets.close();self.service.dataset_library.close();self.tmp.cleanup()
    def cli(self,args,timeout=120):
        if args[:2]==['datasets','create']:
            self.creates+=1;folder=Path(args[args.index('-p')+1]);md=json.loads((folder/'dataset-metadata.json').read_text());ref=md['id']
            self.remote[ref]={'metadata':dict(md,currentVersionNumber=1),'files':{p.name:p.read_bytes() for p in folder.iterdir() if p.is_file() and p.name!='dataset-metadata.json'}}
            if self.lost_reply:raise ValidationError('Network reply lost after remote creation')
            return 'Created'
        ref=args[2]
        if args[:2]==['datasets','metadata']:
            if self.forbidden:raise ValidationError('403 GetDatasetMetadata')
            if ref not in self.remote:raise ValidationError('404 GetDatasetMetadata')
            folder=Path(args[args.index('-p')+1]);folder.mkdir(parents=True,exist_ok=True);(folder/'dataset-metadata.json').write_text(json.dumps(self.remote[ref]['metadata']));return 'ok'
        if args[:2]==['datasets','status']:return 'ready'
        if args[:2]==['datasets','list']:
            return 'ref,title,size,lastUpdated\n'+''.join(f'{ref},Private archive,123,2026-11-08 00:00:00\n' for ref in self.remote)
        if args[:2]==['datasets','files']:
            return 'name,size\n'+''.join(f'{name},{len(data)}\n' for name,data in self.remote[ref]['files'].items())
        if args[:2]==['datasets','download']:
            name=args[args.index('-f')+1];self.downloads.append(name)
            if self.fail_download and len(self.downloads)==self.fail_download:raise ValidationError('Download interrupted')
            (Path(args[args.index('-p')+1])/name).write_bytes(self.remote[ref]['files'][name]);return 'ok'
        raise AssertionError(args)
    def add_job(self,label='1'):
        jid='job_'+label*32;folder=self.service.artifact_root/jid;folder.mkdir()
        (folder/'checkpoint.pth').write_bytes(b'checkpoint'+label.encode());(folder/'metrics.json').write_text('{"dice":0.8}')
        job={'id':jid,'account_id':'account','kernel_slug':'owner/run-'+label,'status':'succeeded','output_dir':str(folder),'created_at':_now(),'result':{'output_pending':False,'output':{'output_dir':str(folder)}}};self.jobs.append(job);return job
    def discover(self):
        with patch('control_plane.artifact_archive.threading.Thread') as thread:
            thread.return_value.is_alive.return_value=False;self.archive.tick()
        self.archive._active.clear();return next(iter(self.archive.data['jobs'].values()))
    def wait_upload(self):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            ops=self.service.datasets.list_operations()
            if ops and ops[0]['status'] in ['succeeded','failed']:
                if not (self.service.datasets.root/'bundles'/ops[0]['id']).exists():return ops[0]
            time.sleep(.01)
        self.fail('upload did not finish')
    def uploaded(self):
        row=self.discover()
        with patch('control_plane.artifact_archive.shutil.disk_usage',return_value=SimpleNamespace(free=100*1024**3)):
            self.archive._run(row)
        self.wait_upload();return row
    def reopen(self):
        self.archive.close();self.service.datasets.close()
        self.service.datasets=DatasetManager(self.service,lambda aid:(self.a,self.cli))
        self.archive=ArtifactArchiver(self.service,lambda aid:(self.a,self.cli))
        return next(iter(self.archive.data['jobs'].values()))
    def test_every_future_completed_run_discovers_without_allowlist_and_no_pending_output(self):
        self.add_job();second=self.add_job('2');second['result']['output_pending']=True
        row=self.uploaded();self.archive._run(row)
        self.assertEqual(row['status'],'verified');self.assertEqual(self.creates,1);self.assertEqual(len(self.proofs),1)
        self.assertTrue(Path(row['output_dir'],'checkpoint.pth').exists());self.assertEqual(self.archive.data['config']['include_job_ids'],[])
        self.assertFalse(self.archive.data['jobs'][second['id']]['output_ready'])
        self.assertEqual(self.archive.data['jobs'][second['id']]['parts'],[])
        self.assertTrue(self.proofs[0]['private_verified']);self.assertFalse(self.proofs[0]['local_evicted'])
        self.assertTrue(self.archive.data['restore']);self.assertFalse(list(Path(row['parts'][0]['stage']).glob('*.bin')))
        second['result']['output_pending']=False;self.discover();self.assertIn(second['id'],self.archive.data['jobs'])
    def test_restart_after_remote_create_lost_reply_verifies_without_reupload(self):
        self.lost_reply=True;self.add_job();self.uploaded();row=self.reopen();self.archive._run(row)
        self.assertEqual(row['status'],'verified');self.assertEqual(self.creates,1)
    def test_restart_mid_readback_reuses_only_same_version_verified_files(self):
        self.add_job();row=self.uploaded();self.fail_download=2;self.archive._run(row)
        first=self.downloads[0];self.assertEqual(row['status'],'waiting_reconciliation');self.assertEqual(len(row['parts'][0]['verified']),1)
        row=self.reopen();self.fail_download=0;self.archive._run(row)
        self.assertEqual(row['status'],'verified');self.assertEqual(self.creates,1);self.assertEqual(self.downloads.count(first),1)
    def test_version_change_invalidates_partial_readback(self):
        self.add_job();row=self.uploaded();self.fail_download=2;self.archive._run(row);first=self.downloads[0]
        self.remote[row['parts'][0]['dataset_ref']]['metadata']['currentVersionNumber']=2
        row=self.reopen();self.fail_download=0;self.archive._run(row)
        self.assertEqual(row['status'],'verified');self.assertEqual(self.downloads.count(first),2)
    def test_403_never_replays_create_and_preserves_original(self):
        self.add_job();row=self.uploaded();self.forbidden=True;row=self.reopen();self.archive._run(row);self.archive._run(row)
        self.assertEqual(row['status'],'waiting_reconciliation');self.assertEqual(self.creates,1);self.assertFalse(self.proofs)
        self.assertTrue(Path(row['output_dir'],'checkpoint.pth').exists())
    def test_corrupt_remote_bytes_never_mark_verified(self):
        self.add_job();row=self.uploaded();ref=row['parts'][0]['dataset_ref'];name=row['parts'][0]['files'][0]['name'];data=self.remote[ref]['files'][name];self.remote[ref]['files'][name]=b'x'*len(data)
        self.archive._run(row);self.assertEqual(row['status'],'waiting_reconciliation');self.assertFalse(self.proofs)
    def test_restart_before_remote_create_resumes_persisted_source_plan(self):
        self.add_job();row=self.discover()
        with patch('control_plane.artifact_archive.shutil.disk_usage',return_value=SimpleNamespace(free=100*1024**3)),patch('control_plane.datasets.threading.Thread'):
            self.archive._run(row)
        self.assertEqual(self.creates,0);row=self.reopen();self.archive._run(row);self.wait_upload();self.archive._run(row)
        self.assertEqual(row['status'],'verified');self.assertEqual(self.creates,1)
    def test_source_change_after_plan_never_uploads_changed_bytes(self):
        self.add_job();row=self.discover();self.archive._plan(row)
        Path(row['output_dir'],'checkpoint.pth').write_bytes(b'new contents')
        with patch('control_plane.artifact_archive.shutil.disk_usage',return_value=SimpleNamespace(free=100*1024**3)):
            self.archive._run(row)
        self.assertEqual(row['status'],'waiting_reconciliation');self.assertEqual(self.creates,0)
    def test_completed_job_cannot_archive_path_outside_managed_outputs(self):
        job=self.add_job();job['result']['output']['output_dir']=str(self.root)
        row=self.discover();self.archive._run(row)
        self.assertEqual(row['status'],'waiting_reconciliation');self.assertEqual(self.creates,0)
    def test_enabled_setting_survives_restart_and_excludes_old_history(self):
        old=self.add_job();old['created_at']='2000-01-01T00:00:00+00:00'
        with patch('control_plane.artifact_archive.threading.Thread'):self.archive.tick()
        self.assertFalse(self.archive.data['jobs']);self.archive.close()
        self.archive=ArtifactArchiver(self.service,lambda aid:(self.a,self.cli))
        self.assertTrue(self.archive.snapshot()['config']['enabled'])
        self.archive.configure({'include_job_ids':[old['id']]});row=self.discover();self.assertEqual(row['job_id'],old['id'])
    def test_restart_with_pending_output_download_collects_before_archive(self):
        job=self.add_job();job['result']['output_pending']=True;row=self.discover()
        self.assertFalse(row['output_ready']);row=self.reopen();calls=[]
        def collect(ids,**kw):
            calls.extend(ids);job['result']['output_pending']=False
        self.service._reconcile_recovered_jobs=collect
        with patch('control_plane.artifact_archive.shutil.disk_usage',return_value=SimpleNamespace(free=100*1024**3)):
            self.archive._run(row)
        self.wait_upload();self.archive._run(row)
        self.assertEqual(calls,[job['id']]);self.assertEqual(row['status'],'verified');self.assertEqual(self.creates,1)
    def test_startup_and_archive_share_one_download_into_output_directory(self):
        job=self.add_job();job['result']['output_pending']=True;calls=[]
        def download(*args):
            calls.append(job['id']);time.sleep(.03)
            return {'output_dir':job['output_dir'],'files':['checkpoint.pth','metrics.json']}
        self.service._output_lock_guard=threading.Lock();self.service._output_locks={};self.service._output_cache={};self.service.adapter=SimpleNamespace(output=download)
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:ControlPlaneService._collect_job_output(self.service,job,{},threading.Event()),range(2)))
        self.assertEqual(len(calls),1);self.assertEqual(results[0],results[1])
    def test_app_shutdown_event_reaches_upload_and_readback_cli(self):
        stop=threading.Event();self.service._recovery_stop=stop;self.service.adapter=KaggleCliAdapter()
        self.service.vault=SimpleNamespace(build_subprocess_env=lambda *args:{})
        self.service.scheduler=SimpleNamespace(_redact=lambda value,*args,**kw:value)
        seen=[]
        def run(args,env,event,**kw):seen.append(event);return 'ready'
        self.service.adapter._run=run
        _,cli=_account_cli(self.service,'account');cli(['datasets','status','owner/archive']);stop.set();cli(['datasets','status','owner/archive'])
        self.assertEqual(seen,[stop,stop]);self.assertTrue(seen[-1].is_set())
    def test_real_cli_metadata_without_version_uses_owned_date_and_resumes(self):
        self.add_job();row=self.uploaded();ref=row['parts'][0]['dataset_ref'];self.remote[ref]['metadata'].pop('currentVersionNumber')
        self.fail_download=2;self.archive._run(row);first=self.downloads[0]
        self.assertEqual(row['parts'][0]['remote_stamp'],{'owned_lastUpdated':'2026-11-08 00:00:00'})
        row=self.reopen();self.fail_download=0;self.archive._run(row)
        self.assertEqual(row['status'],'verified');self.assertEqual(self.downloads.count(first),1)
        self.assertEqual(self.proofs[-1]['method'],'full_remote_readback')

if __name__=='__main__':unittest.main()

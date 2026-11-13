import ast
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from cloud_orchestrator import fm_tail as f
from cloud_orchestrator.native_checkpoint import canonical_sha
from cloud_orchestrator.protocol import digest
from tests_backend.test_cloud_coordinator import Store as BasicStore


def plan():
    cfg=dict(name='parent',arm='DF65',seed=1337,start=12000,stop=14400,max_iterations=15000,no_test=True)
    src='from pathlib import Path\nCFG='+repr(cfg)+"\nFILES={'trainer.py':'science'}\nOFFLINE_FILE_ALIASES={}\ndef sha(p):pass\ndef execute(p):pass\nif __name__=='__main__':runtime_worker(Path('.'))\n"
    h=hashlib.sha256(src.encode()).hexdigest()
    parent=dict(id=f.PARENT,account_id=f.ACCOUNT,kernel_slug='datdungdan/df65-msd10-s1337-12000-14400-move-v1',version=1,source_sha256=h)
    return dict(schema=1,id=f.IDENTITY,kind='df65_tail',cap=3,gpu_cap=1,cpu_cap=2,
        account_id=f.ACCOUNT,username='datdungdan',no_test=True,no_sheet=True,created_at=0,expires_at=86400,
        boundaries=[14400,15000],parent=parent,parent_source=src,parent_contract=dict(config=cfg,source_sha256=h,job_id=f.PARENT),
        initial_files={'trainer.py':'science'},tail_estimate_hours=[2,3],runtime_dataset='owner/runtime',
        training_datasets=['owner/runtime','owner/data','owner/fm'],native_worker='',native_worker_sha256=hashlib.sha256(b'').hexdigest())


def native(contract):
    return dict(status='PASS',contract_sha256=canonical_sha(contract),job_id=contract['job_id'],seed=1337,
        iteration=contract['config']['stop'],source_sha256=contract['source_sha256'],checkpoint_sha256='b'*64,
        full_state_RNG_history=True,source_split_PASS=True,frozen_ViT_PASS=True,best_weights_PASS=True,
        no_test=True,kernel_output_full_SHA_readback=True,history_prefix={},artifact_files={k:dict(sha256='b'*64,bytes=10)
            for k in ['source_manifest.json','runtime.json','split_guard.json','validation.json','resume_latest.pth',
                      'metrics_history.json','val_h_best_student.pth','val_h_best_teachers.pth']})


class Store(BasicStore):
    def __init__(self,p):
        super().__init__();self.doc=dict(plan_json=json.dumps(p),plan_sha256=digest(p),used=0,
            handover=dict(owner_kind='cloud',exclusive_local_stop_verified=True,local_account_disabled=True,plan_sha256=digest(p)),state={})
    def reserve(self,identity,owner,fence,account,job,key,sha,cap):
        ok=super().reserve(identity,owner,fence,account,job,key,sha,cap)
        if ok:self.outbox[key]['source_sha256']=sha
        return ok


class Transport:
    def __init__(self,p,s):self.p,self.s,self.posts,self.remote=p,s,[],{};self.parent='running';self.ready=True;self.lose=False;self.bad_native=False
    def admission(self,*a):return dict(ready=self.ready,status='ADMISSION_PASS' if self.ready else 'WAIT_OFFICIAL_GPU_QUOTA')
    def observe(self,row):
        if row['id']==f.PARENT:return dict(row,status=self.parent)
        return copy.deepcopy(self.remote[row['id']])
    def submit_once(self,p):
        assert self.s.events[-1]=='INTENT'
        self.posts.append(p);row=dict(id=p['id'],account_id=p['account_id'],kernel_slug=p['kernel_slug'],version=1,kernel_id=100+len(self.posts),source_sha256=p['source_sha256'],status='running')
        self.remote[p['id']]=row
        if self.lose:raise TimeoutError('PRIVATE_MESSAGE')
        return {k:v for k,v in row.items() if k not in {'id','status'}}
    def reconcile(self,p):return self.remote[p['id']]
    def read_json(self,row,name):
        _,contract=f.proposals(self.p,self.s.doc['state'])
        result=native(contract)
        if self.bad_native:result['frozen_ViT_PASS']=False
        return result


class FMTailTests(unittest.TestCase):
    def setUp(self):
        self.p=plan();self.patch=patch.object(f,'PARENT_SHA',self.p['parent']['source_sha256']);self.patch.start()
        self.addCleanup(self.patch.stop);self.s=Store(self.p);self.t=Transport(self.p,self.s)
    def tick(self):return f.cycle(self.s,self.t,now=1000,expected_plan_sha=digest(self.p))
    def start(self):self.t.parent='complete';self.tick()
    def complete(self):self.t.remote[self.s.doc['state']['job']['id']]['status']='complete';self.tick()
    def test_running_parent_never_starts_cpu_or_gpu(self):
        self.tick();self.assertEqual(self.t.posts,[])
        self.assertEqual(self.s.doc['state']['status'],'WAIT_PARENT')
    def test_no_handover_no_submission(self):
        self.s.doc['handover']['exclusive_local_stop_verified']=False;self.start();self.assertEqual(self.t.posts,[])
    def test_full_fixed_chain_is_two_cpu_one_gpu(self):
        self.start();self.complete();self.tick();self.complete();self.tick();self.complete()
        self.assertEqual([p['accelerator'] for p in self.t.posts],['cpu','gpu','cpu'])
        self.assertEqual(self.s.doc['state']['status'],'DONE');self.assertEqual(self.s.doc['used'],3)
        self.assertEqual(self.s.reserved,[]);self.tick();self.assertEqual(len(self.t.posts),3)
    def test_native_failure_closes_branch_before_gpu(self):
        self.start();self.t.bad_native=True;self.complete();self.tick()
        self.assertEqual(self.s.doc['state']['status'],'STOP');self.assertEqual(len(self.t.posts),1)
    def test_wrong_native_parent_cannot_stage_gpu(self):
        n=native(self.p['parent_contract']);n['iteration']=12000
        with self.assertRaises(ValueError):f.validate_native(n,self.p['parent_contract'])
    def test_quota_wait_is_durable_without_reservation_or_post(self):
        self.start();self.complete();self.t.ready=False;self.tick()
        self.assertEqual(len(self.t.posts),1);self.assertEqual(self.s.doc['used'],1)
        self.assertEqual(self.s.doc['state']['status'],'WAIT_OFFICIAL_GPU_QUOTA')
    def test_ambiguous_save_adopts_original_never_replays(self):
        self.t.lose=True;self.start();self.tick();self.assertEqual(len(self.t.posts),1)
        self.assertNotIn('PRIVATE_MESSAGE',str(self.s.errors))
    def test_gpu_failure_never_retried(self):
        self.start();self.complete();self.tick()
        self.t.remote[self.s.doc['state']['job']['id']]['status']='failed';self.tick();self.tick()
        self.assertEqual(len(self.t.posts),2);self.assertEqual(self.s.doc['state']['status'],'STOP')
    def test_no_arbitrary_plan_or_extra_gpu_cap(self):
        self.s.doc['plan_json']=json.dumps(dict(self.p,gpu_cap=2));self.start();self.assertEqual(self.t.posts,[])
    def test_native_worker_must_be_sealed(self):
        p=dict(self.p,native_worker='changed')
        with self.assertRaises(ValueError):f.proposals(p,{})
    def test_deployment_plan_anchor_rejects_modified_firestore_plan(self):
        self.s.doc['plan_sha256']='a'*64;self.start();self.assertEqual(self.t.posts,[])
    def test_cpu_source_stable_across_firestore_map_order(self):
        original,_=f.proposals(self.p,{})
        sorted_plan=json.loads(json.dumps(self.p,sort_keys=True))
        reloaded,_=f.proposals(sorted_plan,{})
        self.assertEqual(original['source_sha256'],reloaded['source_sha256'])
        self.start();self.complete();self.tick();self.complete()
        state=self.s.doc['state'];a,_=f.proposals(self.p,state)
        b,_=f.proposals(sorted_plan,json.loads(json.dumps(state,sort_keys=True)))
        self.assertEqual(a['source_sha256'],b['source_sha256'])
    def test_changed_source_after_unknown_request_never_posts_again(self):
        self.t.lose=True;self.start()
        for intent in self.s.outbox.values():intent['source_sha256']='c'*64
        self.tick();self.assertEqual(len(self.t.posts),1)


if __name__=='__main__':unittest.main()

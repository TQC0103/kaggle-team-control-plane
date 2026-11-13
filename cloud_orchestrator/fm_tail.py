"""Finite migration of one approved DF65 tail; no new arm or seed.

Parent14400 -> native CPU -> original GPU15000 -> native CPU -> DONE.
Private same-owner Kaggle outputs are retained and fully read back on CPU.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
import time
from uuid import uuid4

from .continuation import literal, stage_child
from .native_checkpoint import canonical_sha
from .protocol import digest

IDENTITY='df65-msd-s1337-final-cloud-v1'
ACCOUNT='acct_592ed92e27544d1fb586559e09a30308'
PARENT='job_87576974f6f74c928cad6a22aaa8cdb7'
PARENT_SHA='2db38c475e936f70c0f115857b71a2fff905e9219243479ebfa2cc166d42d5e2'


def validate_plan(plan):
    if (plan.get('schema')!=1 or plan.get('id')!=IDENTITY or plan.get('kind')!='df65_tail'
            or plan.get('cap')!=3 or plan.get('gpu_cap')!=1 or plan.get('cpu_cap')!=2
            or plan.get('account_id')!=ACCOUNT or plan.get('username')!='datdungdan'
            or plan.get('no_test') is not True or plan.get('no_sheet') is not True
            or plan.get('expires_at',0)-plan.get('created_at',0)>30*86400
            or plan.get('expires_at',0)<=plan.get('created_at',0)
            or plan.get('boundaries')!=[14400,15000]
            or plan.get('parent',{}).get('id')!=PARENT
            or plan['parent'].get('source_sha256')!=PARENT_SHA
            or plan['parent'].get('account_id')!=ACCOUNT
            or plan['parent'].get('version')!=1
            or plan['parent'].get('kernel_slug')!='datdungdan/df65-msd10-s1337-12000-14400-move-v1'
            or hashlib.sha256(plan.get('parent_source','').encode()).hexdigest()!=PARENT_SHA):
        raise ValueError('DF65 fixed tail plan mismatch')
    cfg=literal(ast.parse(plan['parent_source']),'CFG')
    contract=plan['parent_contract']
    if (cfg!=contract['config'] or cfg['arm']!='DF65' or cfg['seed']!=1337
            or cfg['max_iterations']!=15000 or cfg['stop']!=14400
            or contract['source_sha256']!=PARENT_SHA or contract['job_id']!=PARENT
            or contract.get('known_checkpoint_sha256')
            or plan['initial_files']!=literal(ast.parse(plan['parent_source']),'FILES')
            or plan['tail_estimate_hours'][1]>10.5):
        raise ValueError('DF65 scientific contract mismatch')


def cpu_source(plan, contract, name):
    """Reuse the original offline bootstrap and pins; CPU native checks only."""
    tree=ast.parse(plan['parent_source'])
    imports='\n'.join(ast.get_source_segment(plan['parent_source'],n) for n in tree.body if isinstance(n,(ast.Import,ast.ImportFrom)))
    funcs='\n\n'.join(ast.get_source_segment(plan['parent_source'],n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['sha','execute'])
    mains=[n for n in tree.body if isinstance(n,ast.If)]
    if len(mains)!=1:raise ValueError('one offline bootstrap entry required')
    main=ast.get_source_segment(plan['parent_source'],mains[0])
    aliases=literal(tree,'OFFLINE_FILE_ALIASES')
    worker=plan['native_worker']
    if hashlib.sha256(worker.encode()).hexdigest()!=plan['native_worker_sha256']:
        raise ValueError('native worker seal drift')
    entry="def runtime_worker(root):\n    namespace={}\n    exec(compile(WORKER,'native_validator.py','exec'),namespace)\n    namespace['run_kernel_output'](root,CONTRACT,Path('/kaggle/input'))\n"
    canonical_contract=json.loads(json.dumps(contract,sort_keys=True,allow_nan=False))
    source=('CFG='+repr(dict(name=name))+'\nCONTRACT='+repr(canonical_contract)+'\nWORKER='+repr(worker)
            +'\nOFFLINE_FILE_ALIASES='+repr(aliases)+'\n'+imports+'\n'+funcs+'\n'+entry+'\n'+main+'\n')
    compile(source,'run.py','exec')
    return source


def proposals(plan,state):
    stage=state.get('stage',0)
    if stage not in {0,1,2}:raise ValueError('fixed three-stage tail only')
    base=dict(account_id=ACCOUNT,is_private=True,enable_internet=False)
    if stage==0:
        name='kcp-native-df65-s1337-i14400-cloud-v1'
        contract=plan['parent_contract']
        source=cpu_source(plan,contract,name)
        base.update(accelerator='cpu',dataset_sources=[plan['runtime_dataset']],
                    kernel_sources=[plan['parent']['kernel_slug']],timeout_seconds=1800)
    elif stage==1:
        name='df65-msd10-s1337-14400-15000-cloud-v1'
        child=stage_child(plan['parent_source'],state['parent_native'],name=name,stop=15000,
                          boundaries=plan['boundaries'],payload_filename='resume_latest.pth')
        source=child['source'];contract=None
        base.update(accelerator='gpu',machine_shape='NvidiaTeslaT4',dataset_sources=plan['training_datasets'],
                    kernel_sources=[plan['parent']['kernel_slug']],timeout_seconds=37800)
    else:
        name='kcp-native-df65-s1337-i15000-cloud-v1'
        training=state['training_job'];cfg=state['training_config']
        contract=deepcopy(plan['parent_contract'])
        contract.update(job_id=training['id'],config=cfg,source_sha256=training['source_sha256'],
                        parent_history_prefix=state['parent_native']['history_prefix'])
        source=cpu_source(plan,contract,name)
        base.update(accelerator='cpu',dataset_sources=[plan['runtime_dataset']],
                    kernel_sources=[training['kernel_slug']],timeout_seconds=1800)
    base.update(id='job_'+digest(dict(program=plan['id'],stage=stage))[:32],kernel_slug='datdungdan/'+name,
                source=source,source_sha256=hashlib.sha256(source.encode()).hexdigest())
    return base,contract


def validate_native(proof,contract):
    for key,value in dict(status='PASS',contract_sha256=canonical_sha(contract),job_id=contract['job_id'],
                          seed=1337,iteration=contract['config']['stop'],source_sha256=contract['source_sha256']).items():
        if proof.get(key)!=value:raise ValueError('native receipt identity mismatch')
    checks=['full_state_RNG_history','source_split_PASS','frozen_ViT_PASS','best_weights_PASS','no_test','kernel_output_full_SHA_readback']
    if any(proof.get(k) is not True for k in checks):raise ValueError('native receipt failed or incomplete')
    files=proof.get('artifact_files',{})
    required={'source_manifest.json','runtime.json','split_guard.json','validation.json','resume_latest.pth',
              'metrics_history.json','val_h_best_student.pth','val_h_best_teachers.pth'}
    if set(files)!=required or files['resume_latest.pth']['sha256']!=proof.get('checkpoint_sha256'):
        raise ValueError('missing complete private output SHA readback')


def cycle(store,transport,now=None,expected_plan_sha=None):
    now=time.time() if now is None else now
    owner=uuid4().hex;fence=store.claim(owner,now)
    if fence is None:return dict(status='SKIP_LEASE')
    outcomes={}
    for document in store.programs(1):
        identity=IDENTITY
        try:
            plan=json.loads(document['plan_json']);validate_plan(plan)
            if (document.get('_document_id',IDENTITY)!=IDENTITY or document.get('plan_sha256')!=digest(plan)
                    or (expected_plan_sha is not None and expected_plan_sha!=digest(plan))):
                raise ValueError('sealed handover plan mismatch')
            state=deepcopy(document.get('state',{}))
            if state.get('status') in {'DONE','STOP','EXPIRED'}:
                outcomes[identity]=state['status'];continue
            handover=document.get('handover') or {}
            if (handover.get('owner_kind')!='cloud' or handover.get('exclusive_local_stop_verified') is not True
                    or handover.get('local_account_disabled') is not True
                    or handover.get('plan_sha256')!=digest(plan)):
                outcomes[identity]='WAIT_SINGLE_WRITER_HANDOVER';continue
            if now>=plan['expires_at']:
                state.update(status='EXPIRED');store.save(identity,owner,fence,state);continue
            stage=state.get('stage',0)
            p,contract=proposals(plan,state)
            if stage==0 and not state.get('job'):
                parent=transport.observe(plan['parent'])
                if parent['status'] in {'failed','cancelled'}:
                    state.update(status='STOP',reason='PARENT_TERMINAL_FAILURE');store.save(identity,owner,fence,state);continue
                if parent['status']!='complete':
                    state.update(status='WAIT_PARENT',last_observation=parent)
                    store.save(identity,owner,fence,state);outcomes[identity]='WAIT_PARENT';continue
            job=state.get('job')
            if job:
                observed=transport.observe(job)
                if observed['status'] in {'failed','cancelled'}:
                    state.update(status='STOP',reason='REMOTE_TERMINAL_FAILURE')
                elif observed['status']=='complete':
                    if stage in {0,2}:
                        result=transport.read_json(job,'native_receipt.json')
                        try:validate_native(result,contract)
                        except ValueError:
                            state.update(status='STOP',reason='NATIVE_VALIDATION_FAILED');store.save(identity,owner,fence,state);continue
                        state['parent_native' if stage==0 else 'final_native']=result
                    else:
                        state['training_job']=job
                        state['training_config']=literal(ast.parse(p['source']),'CFG')
                    state.setdefault('completed',[]).append(dict(stage=stage,job=job))
                    store.release_slot(ACCOUNT,p['id'])
                    state.update(status='DONE' if stage==2 else 'READY_NEXT_STAGE',stage=stage if stage==2 else stage+1,job=None)
                store.save(identity,owner,fence,state);outcomes[identity]=state.get('status',observed['status']);continue
            # One immutable intent per stage even if an infrastructure change
            # accidentally changes source rendering after an unknown response.
            key=digest(dict(program=identity,proposal=p['id']))
            intent=store.intent(key)
            if intent:
                if intent.get('source_sha256')!=p['source_sha256']:
                    raise ValueError('source changed after persisted stage intent')
                observed=transport.reconcile(p)
                if observed['status'] not in {'queued','running','complete','failed','cancelled'}:
                    raise ValueError('uncertain exact mutation')
                job={k:observed[k] for k in ['account_id','kernel_slug','version','kernel_id','source_sha256']};job['id']=p['id']
            else:
                admission=transport.admission(plan,state,p)
                if not admission['ready']:
                    state.update(status=admission['status'],admission=admission)
                    store.save(identity,owner,fence,state);outcomes[identity]=admission['status'];continue
                if not store.reserve(identity,owner,fence,ACCOUNT,p['id'],key,p['source_sha256'],3):
                    outcomes[identity]='WAIT_RESERVATION';continue
                saved=transport.submit_once(p);job=dict(id=p['id'],**saved);store.accepted(key,job)
                observed=transport.observe(job)
                if observed['status'] not in {'queued','running','complete'}:raise ValueError('remote request not verified')
            store.receipt(key,job)
            state.update(status='RUNNING',job=job)
            store.save(identity,owner,fence,state);outcomes[identity]='REMOTE_VERIFIED'
        except Exception as exc:
            store.error(identity,type(exc).__name__);outcomes[identity]='RECONCILE_NO_REPLAY'
    store.finish(owner,fence)
    return dict(status='TICK_COMPLETE',programs=outcomes,scope='DF65_seed1337_original_final_tail_only')

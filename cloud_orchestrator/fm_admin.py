"""Public-metadata administration of the one approved DF65 handover."""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlparse

from .connection import PROJECT
from .fm_tail import IDENTITY,ACCOUNT,validate_plan
from .protocol import digest


def load(root):
    p=json.loads((root/'plan.json').read_text());validate_plan(p)
    return p


def publish(root):
    from google.cloud import firestore
    from google.api_core.exceptions import AlreadyExists
    plan=load(root);db=firestore.Client(project=PROJECT)
    ref=db.collection('kcp_fm_programs').document(IDENTITY)
    try:ref.create(dict(plan_json=json.dumps(plan,sort_keys=True),plan_sha256=digest(plan),used=0,state=dict(status='WAIT_HANDOVER')),retry=None,timeout=10)
    except AlreadyExists:
        if ref.get(timeout=10).to_dict().get('plan_sha256')!=digest(plan):raise ValueError('existing plan mismatch')
    acct=db.collection('kcp_fm_accounts').document(ACCOUNT)
    try:acct.create(dict(enabled=True,reserved_jobs=[],purpose=IDENTITY),retry=None,timeout=10)
    except AlreadyExists:
        if acct.get(timeout=10).to_dict().get('purpose')!=IDENTITY:raise ValueError('existing account ownership mismatch')
    return dict(status='PUBLISHED_NOT_ACTIVE',plan_sha256=digest(plan))


def activate(root):
    from google.cloud import firestore
    import urllib.request
    plan=load(root);proof=json.loads((root/'LOCAL_HANDOVER_PASS.json').read_text())
    if (proof.get('status')!='PASS' or proof.get('plan_sha256')!=digest(plan)
            or proof.get('exclusive_local_stop_verified') is not True
            or proof.get('local_account_disabled') is not True
            or not 0<=time.time()-proof.get('created_at',0)<300):
        raise ValueError('fresh exclusive local stop proof required')
    account=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/accounts/'+ACCOUNT,timeout=10))['account']
    if account['state']!='disabled':raise ValueError('local account dispatch is not disabled')
    db=firestore.Client(project=PROJECT);ref=db.collection('kcp_fm_programs').document(IDENTITY)
    snapshot=ref.get(timeout=10).to_dict()
    if snapshot.get('plan_sha256')!=digest(plan):raise ValueError('remote exact plan mismatch')
    handover=dict(owner_kind='cloud',exclusive_local_stop_verified=True,local_account_disabled=True,
                  plan_sha256=digest(plan),proof_sha256=digest(proof),activated_at=time.time())
    if snapshot.get('handover'):
        if snapshot['handover'].get('proof_sha256')!=digest(proof):raise ValueError('existing handover differs; reconcile')
        return dict(status='ALREADY_ACTIVE',handover=snapshot['handover'])
    ref.update(dict(handover=handover),retry=None,timeout=10)
    (root/'CLOUD_HANDOVER_RECEIPT.json').write_text(json.dumps(handover,indent=2))
    return dict(status='ACTIVE',handover=handover)


def status(root):
    from google.cloud import firestore
    plan=load(root);db=firestore.Client(project=PROJECT)
    r=db.collection('kcp_fm_programs').document(IDENTITY).get(timeout=10).to_dict()
    if r is None:return dict(status='NOT_PUBLISHED')
    if r.get('plan_sha256')!=digest(plan):raise ValueError('remote plan mismatch')
    event=db.collection('kcp_fm_coordinator_events').document(IDENTITY).get(timeout=10).to_dict()
    result=dict(plan_sha256=r['plan_sha256'],state=r.get('state'),used=r.get('used'),handover=r.get('handover'),event=event)
    (root/'latest_cloud_status.json').write_text(json.dumps(result,indent=2))
    return result


def tick(root,service_file,source_sha):
    import google.auth
    from google.auth.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    import requests
    service=json.loads(Path(service_file).read_text(encoding='utf-8-sig'));url=service['status']['url']
    host=urlparse(url)
    if host.scheme!='https' or not (host.hostname or '').startswith('kcp-cloud-training-') or not host.hostname.endswith('.run.app'):
        raise ValueError('unexpected private training service')
    unauth=requests.get(url+'/health',timeout=15,allow_redirects=False)
    if unauth.status_code not in {401,403}:raise ValueError('service is not private')
    user,_=google.auth.default()
    class Identity(Credentials):
        def refresh(self,request):
            user.refresh(request)
            if not getattr(user,'id_token',None):raise ValueError('managed user identity unavailable')
            self.token,self.expiry=user.id_token,user.expiry
    with AuthorizedSession(Identity()) as s:
        health=s.get(url+'/health',timeout=25,allow_redirects=False)
        expected=dict(mode='df65_tail_v1',source_sha256=source_sha,scope='DF65_seed1337_14400_to_15000',gpu_cap=1,cpu_cap=2)
        if health.status_code!=200 or health.json()!=expected:raise ValueError('deployed source/scope mismatch')
        response=s.post(url+'/tick',data=b'',timeout=65,allow_redirects=False)
        if response.status_code!=200:raise ValueError('tick response unknown; read state before further action')
        result=dict(at=time.time(),private_status=unauth.status_code,health=health.json(),tick=response.json())
    (root/f'tick_{int(time.time())}.json').write_text(json.dumps(result,indent=2))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['publish','activate','status','tick'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--service-file');p.add_argument('--source-sha')
    a=p.parse_args()
    r=tick(a.root,a.service_file,a.source_sha) if a.action=='tick' else globals()[a.action](a.root)
    print(json.dumps(r,indent=2))


if __name__=='__main__':main()

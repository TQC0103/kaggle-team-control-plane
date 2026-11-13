"""Operator CLI for exactly two CPU-only cloud transport fixtures.

No scientific ownership transfer or desktop credential reads.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

from .connection import PROJECT
from .cpu_fixture import proposal, validate_plan
from .protocol import digest


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    if (root/'plan.json').exists():
        raise ValueError('fixture is immutable; reconcile existing plan')
    now = time.time()
    plan = dict(schema=1, kind='cpu_fixture', id='cpu-fixture-'+uuid4().hex[:12],
                cap=2, account_id='acct_d321057bf0954d048b448711e0efed7f',
                username='tqc0103', created_at=now, expires_at=now+5400)
    validate_plan(plan)
    (root/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    for step in [1, 2]:
        p = proposal(plan, step)
        (root/f'step{step}.py').write_text(p['source'])
        (root/f'step{step}.json').write_text(json.dumps({k:v for k,v in p.items() if k!='source'}, indent=2)+'\n')
    return dict(status='PREPARED', plan_sha256=digest(plan), cap=2, gpu_hours=0)


def publish(root):
    from google.cloud import firestore
    from google.api_core.exceptions import AlreadyExists
    plan = json.loads((root/'plan.json').read_text())
    validate_plan(plan)
    if time.time() >= plan['expires_at']:
        raise ValueError('fixture expired before publication')
    db = firestore.Client(project=PROJECT)
    ref = db.collection('kcp_cloud_programs').document(plan['id'])
    row = dict(plan=plan, plan_sha256=digest(plan), state=dict(status='READY'), used=0)
    try:
        ref.create(row, retry=None, timeout=10)
    except AlreadyExists:
        if ref.get(retry=None, timeout=10).to_dict().get('plan_sha256') != digest(plan):
            raise ValueError('existing fixture differs')
    account = db.collection('kcp_cloud_accounts').document(plan['account_id'])
    try:
        account.create(dict(enabled=True, reserved_jobs=[], purpose='cpu_fixture_only'), retry=None, timeout=10)
    except AlreadyExists:
        existing = account.get(retry=None, timeout=10).to_dict()
        if not existing.get('enabled') or existing.get('purpose') != 'cpu_fixture_only':
            raise ValueError('account cannot be claimed for CPU fixtures')
    return dict(status='PUBLISHED', plan_sha256=digest(plan), scientific_handover=False)


def status(root):
    from google.cloud import firestore
    plan = json.loads((root/'plan.json').read_text())
    validate_plan(plan)
    db = firestore.Client(project=PROJECT)
    row = db.collection('kcp_cloud_programs').document(plan['id']).get(retry=None, timeout=10).to_dict()
    event = db.collection('kcp_cloud_coordinator_events').document(plan['id']).get(retry=None, timeout=10).to_dict()
    result = dict(program=row, event=event)
    (root/'latest_status.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


def tick(root, service_file, source_sha):
    import google.auth
    from google.auth.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    import requests
    from urllib.parse import urlparse
    service = json.loads(Path(service_file).read_text(encoding='utf-8-sig'))
    url = service['status']['url']
    host = urlparse(url)
    if host.scheme!='https' or not (host.hostname or '').startswith('kcp-cloud-coordinator-') or not host.hostname.endswith('.run.app'):
        raise ValueError('unexpected coordinator service URL')
    unauth = requests.get(url+'/health', timeout=15, allow_redirects=False)
    if unauth.status_code not in {401,403}:
        raise ValueError('coordinator must remain private')
    user, _ = google.auth.default()
    class Identity(Credentials):
        def refresh(self, request):
            user.refresh(request)
            if not getattr(user,'id_token',None):
                raise ValueError('Google user identity unavailable')
            self.token, self.expiry = user.id_token, user.expiry
    with AuthorizedSession(Identity()) as session:
        health = session.get(url+'/health',timeout=20,allow_redirects=False)
        expected = dict(mode='cpu_fixture',scientific_submission_enabled=False,cpu_fixture_cap=2,source_sha256=source_sha)
        if health.status_code!=200 or health.json()!=expected:
            raise ValueError('coordinator deployed source mismatch')
        response = session.post(url+'/tick',data=b'',timeout=65,allow_redirects=False)
        if response.status_code!=200:
            raise ValueError('coordinator tick did not return a confirmed response; inspect Firestore, never replay mutations')
        result = dict(at=time.time(),health=health.json(),tick=response.json(),private_status=unauth.status_code)
    (root/f'tick_{int(time.time())}.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['prepare','publish','status','tick'])
    p.add_argument('--root',required=True,type=Path)
    p.add_argument('--service-file')
    p.add_argument('--source-sha')
    a=p.parse_args()
    if a.action=='tick':result=tick(a.root,a.service_file,a.source_sha)
    else:result=globals()[a.action](a.root)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()

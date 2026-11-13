"""Two stdlib-only CPU jobs to prove cloud parent-before-child execution.

No research data, trainer, inference, GPU, secrets or arbitrary source template.
"""
from __future__ import annotations

import hashlib
import json
import re

from .protocol import digest


def receipt(plan, step):
    return dict(schema=1, fixture=plan['id'], counter=step,
                parent_sha256=None if step == 1 else digest(receipt(plan, 1)))


def validate_plan(plan):
    if (plan.get('schema') != 1 or plan.get('kind') != 'cpu_fixture'
            or not re.fullmatch(r'cpu-fixture-[a-f0-9]{12}', str(plan.get('id', '')))
            or plan.get('cap') != 2 or plan.get('account_id') != 'acct_d321057bf0954d048b448711e0efed7f'
            or plan.get('username') != 'tqc0103'
            or type(plan.get('created_at')) not in {float, int}
            or type(plan.get('expires_at')) not in {float, int}
            or not 0 < plan['expires_at']-plan['created_at'] <= 5400):
        raise ValueError('invalid bounded CPU fixture plan')


def proposal(plan, step):
    validate_plan(plan)
    if step not in {1, 2}:
        raise ValueError('two CPU fixture steps only')
    suffix = plan['id'].split('-')[-1]
    name = f'kcp-cloud-echo-{suffix}-{step:02d}-v1'
    expected_parent = receipt(plan, 1) if step == 2 else None
    source = '''"""Infrastructure CPU fixture: no training/inference or research inputs."""
import os,sys,json,hashlib,subprocess,shutil
from pathlib import Path
NAME = __NAME__
EXPECTED = __EXPECTED__
PARENT = __PARENT__
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
root=Path('/kaggle/working')/NAME
root.mkdir(exist_ok=True)
if os.environ.get('KCP_CPU_FIXTURE_VENV') != '1':
    venv=Path('/kaggle/working')/(NAME+'-venv')
    assert venv.parent==Path('/kaggle/working') and venv.name.startswith('kcp-cloud-echo-')
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True)
    try:
        env=dict(os.environ,KCP_CPU_FIXTURE_VENV='1',PYTHONNOUSERSITE='1')
        subprocess.run([str(venv/'bin/python'),str(Path(__file__).resolve())],env=env,check=True)
    finally:
        if venv.exists():shutil.rmtree(venv)
else:
    if PARENT is not None:
        files=list(Path('/kaggle/input').rglob('echo_receipt.json'))
        assert len(files)==1
        actual=json.loads(files[0].read_text())
        assert actual==PARENT and digest(actual)==EXPECTED['parent_sha256']
    (root/'echo_receipt.json').write_text(json.dumps(EXPECTED,sort_keys=True,indent=2)+'\\n')
    print('CPU_FIXTURE_COMPLETE',EXPECTED['counter'],flush=True)
'''
    source = source.replace('__NAME__', repr(name)).replace('__EXPECTED__', repr(receipt(plan, step))).replace('__PARENT__', repr(expected_parent))
    compile(source, 'run.py', 'exec')
    parent_slug = f"{plan['username']}/kcp-cloud-echo-{suffix}-01-v1"
    return dict(id='job_'+digest(dict(plan=plan['id'], step=step))[:32], account_id=plan['account_id'],
                kernel_slug=plan['username']+'/'+name, source=source,
                source_sha256=hashlib.sha256(source.encode()).hexdigest(), accelerator='cpu',
                is_private=True, enable_internet=False, dataset_sources=[],
                kernel_sources=[] if step == 1 else [parent_slug], timeout_seconds=600)

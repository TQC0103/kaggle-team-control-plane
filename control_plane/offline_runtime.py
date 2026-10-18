"""Stdlib offline runner. This module is embedded in each generated entrypoint."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import venv

def run(spec, workload):
    root=Path('/kaggle/working') if Path('/kaggle/working').is_dir() else Path.cwd()
    root=root/'kcp-offline';root.mkdir(exist_ok=True)
    abi='cp'+str(sys.version_info.major)+str(sys.version_info.minor)
    expected=spec['python_abi']
    if expected!='native' and expected!=abi:
        raise RuntimeError(f'Offline Python ABI mismatch: expected {expected}, got {abi}; prepackage the correct Linux interpreter, never fetch it at runtime')
    isolated=root/'venv';strategy=spec['strategy']
    if strategy not in ('native','wheelhouse'):raise RuntimeError('Unknown offline runtime strategy')
    if isolated.exists():
        raise RuntimeError('Offline venv already exists; use a fresh job working directory')
    venv.EnvBuilder(system_site_packages=strategy=='native',with_pip=strategy=='wheelhouse').create(isolated)
    python=isolated/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','PIP_INDEX_URL','PIP_EXTRA_INDEX_URL','PIP_TRUSTED_HOST'):env.pop(k,None)
    env.update(PYTHONNOUSERSITE='1',PIP_NO_INDEX='1',PIP_DISABLE_PIP_VERSION_CHECK='1',PIP_CONFIG_FILE=os.devnull,PIP_NO_CACHE_DIR='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',WANDB_MODE='offline')
    if strategy=='wheelhouse':
        base=Path(spec.get('input_root','/kaggle/input'));candidates=list(base.rglob(spec['wheelhouse_manifest']))
        assert len(candidates)==1,'Expected exactly one offline wheelhouse manifest'
        manifest=candidates[0];assert hashlib.sha256(manifest.read_bytes()).hexdigest()==spec['wheelhouse_manifest_sha256'],'Wheel manifest SHA mismatch'
        files=json.loads(manifest.read_text())['files'];directory=manifest.parent
        assert 'requirements.lock' in files
        for name,digest in files.items():
            original=directory/name;p=original.resolve()
            assert p.is_relative_to(directory.resolve()) and p.is_file() and not original.is_symlink(),'Unsafe wheelhouse path'
            assert hashlib.sha256(p.read_bytes()).hexdigest()==digest,('Wheelhouse SHA mismatch',name)
            if name.endswith('.whl'):
                assert 'win_' not in name and 'macosx' not in name,'Offline wheelhouse must target Linux'
        lock=(directory/'requirements.lock').read_text().replace('\\\n',' ')
        for line in lock.splitlines():
            line=line.strip()
            if not line or line.startswith('#'):continue
            assert re.fullmatch(r'[A-Za-z0-9_.-]+==[^\s;@]+(?:\s+--hash=sha256:[a-f0-9]{64})+',line),'Use only exact name==version plus SHA256 hashes; no URLs or pip directives'
        actual={p.relative_to(directory).as_posix() for p in directory.rglob('*.whl')}
        assert actual=={n for n in files if n.endswith('.whl')},'Unmanifested wheel files'
        subprocess.run([str(python),'-I','-m','pip','install','--no-index','--find-links',str(directory),'--require-hashes','--only-binary=:all:','-r',str(directory/'requirements.lock')],env=env,check=True)
    code="""import importlib.metadata,json,platform,sys
from pathlib import Path
expected=json.loads(sys.argv[1])
actual={name:importlib.metadata.version(name) for name in expected}
assert all(actual[k]==v for k,v in expected.items()),('Offline package mismatch',actual,expected)
Path(sys.argv[2]).write_text(json.dumps(dict(python=sys.version,platform=platform.platform(),packages=actual,strategy=sys.argv[3]),indent=2))
"""
    subprocess.run([str(python),'-I','-c',code,json.dumps(spec.get('expected_packages',{})),str(root/'runtime.json'),strategy],env=env,check=True)
    assert hashlib.sha256(workload.encode()).hexdigest()==spec['workload_sha256'],'Embedded workload SHA mismatch'
    target=root/'workload.py';target.write_text(workload,encoding='utf-8')
    try:
        subprocess.run([str(python),'-I',str(target)],env=env,cwd=root,check=True)
    finally:
        # Keep results/diagnostics, not an entire environment in the Kaggle output.
        shutil.rmtree(isolated)


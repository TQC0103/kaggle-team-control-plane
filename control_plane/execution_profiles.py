"""Explicit advanced shapes and offline source contracts; no entitlement claims."""
import ast
import hashlib
import json
from pathlib import Path
import re
from .errors import ValidationError

ADVANCED_GPU_SHAPES={'NvidiaL4','NvidiaL4X1','NvidiaTeslaA100','NvidiaH100','NvidiaRtxPro6000'}
GPU_SHAPES={'NvidiaTeslaT4'}|ADVANCED_GPU_SHAPES
CATALOG_SOURCE='https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md'
def catalog():
    return dict(source=CATALOG_SOURCE,shapes=[dict(machine_shape=s,competition_profile_required=s in ADVANCED_GPU_SHAPES,
        availability='account/competition dependent; listed support is not entitlement') for s in sorted(GPU_SHAPES)])
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def validate_profile(source, machine_shape, remaining_hours=None):
    if machine_shape not in GPU_SHAPES|{'CPU','TpuV38'}:
        raise ValidationError('Unsupported accelerator shape')
    source=Path(source);profile=source/'offline-profile.json';advanced=machine_shape in ADVANCED_GPU_SHAPES
    if not profile.is_file():
        if advanced:raise ValidationError('Advanced GPU needs offline-profile.json and a reviewed competition attachment')
        return None
    try:
        p=json.loads(profile.read_text(encoding='utf-8'));metadata=json.loads((source/'kernel-metadata.json').read_text(encoding='utf-8'))
        assert isinstance(p,dict) and isinstance(metadata,dict),'Profile and metadata must be JSON objects'
        assert p['schema']==1 and p['mode'] in ('offline','competition_offline')
        assert metadata.get('enable_internet') is False,'Set enable_internet=false explicitly'
        assert p['machine_shape']==machine_shape,'Profile/request accelerator mismatch'
        code=source/metadata['code_file'];assert code.resolve().is_relative_to(source.resolve()) and not code.is_symlink()
        assert sha(code)==p['entrypoint_sha256'],'Offline entrypoint hash mismatch'
        constants={n.targets[0].id:ast.literal_eval(n.value) for n in ast.parse(code.read_text()).body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ('OFFLINE_SPEC','WORKLOAD_CODE','OFFLINE_RUNNER')}
        assert constants['OFFLINE_SPEC']==p['runtime'],'Embedded runtime spec drift'
        assert hashlib.sha256(constants['WORKLOAD_CODE'].encode()).hexdigest()==p['runtime']['workload_sha256']
        runner=Path(__file__).with_name('offline_runtime.py').read_text(encoding='utf-8')
        assert constants['OFFLINE_RUNNER']==runner,'Use the current offline runner; never install online'
        assert p['runtime']['strategy'] in ('native','wheelhouse')
        assert re.fullmatch(r'cp\d{2,3}|native',p['runtime']['python_abi'])
        assert isinstance(p['runtime'].get('expected_packages',{}),dict)
        if p['runtime']['strategy']=='wheelhouse':
            assert p['runtime']['python_abi']!='native','Lock wheelhouse ABI'
            assert re.fullmatch(r'[a-zA-Z0-9_.-]+\.json',p['runtime']['wheelhouse_manifest'])
            assert re.fullmatch(r'[a-f0-9]{64}',p['runtime']['wheelhouse_manifest_sha256'])
            assert p['runtime']['wheelhouse_dataset_ref'] in metadata.get('dataset_sources',[])
        if advanced:
            assert p['mode']=='competition_offline','Advanced shapes require competition_offline mode'
        if p['mode']=='competition_offline':
            slug=p['competition_slug'];assert re.fullmatch(r'[a-z0-9][a-z0-9-]{1,127}',slug)
            assert slug in metadata.get('competition_sources',[]),'Competition not attached to notebook'
            assert p['purpose']=='competition_workload','Unrelated research needs separately verified Kaggle authorization'
            assert p['rules_url'].startswith('https://www.kaggle.com/competitions/'+slug+'/'),'Record competition rules source'
            assert isinstance(p['quota_multiplier'],(int,float)) and p['quota_multiplier']>=1
            assert isinstance(p['estimated_session_minutes'],(int,float)) and 0<p['estimated_session_minutes']<=720
            cost=p['estimated_session_minutes']/60*p['quota_multiplier']
            if remaining_hours is not None:assert remaining_hours>=cost,'Insufficient quota for declared competition cost'
        return dict(profile_sha256=sha(profile),metadata_sha256=sha(source/'kernel-metadata.json'),entrypoint_sha256=p['entrypoint_sha256'],mode=p['mode'],competition_slug=p.get('competition_slug'),
            runtime_strategy=p['runtime']['strategy'],internet=False,entitlement='not established by profile; Kaggle is authoritative')
    except (KeyError,AssertionError,ValueError,TypeError,OSError,SyntaxError) as e:
        raise ValidationError('Offline/competition preflight failed: '+str(e)) from None

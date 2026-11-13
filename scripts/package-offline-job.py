"""Generate one self-contained entrypoint; companion .py files are not uploaded by Kaggle."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from control_plane.execution_profiles import validate_profile, GPU_SHAPES
def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--workload',type=Path,required=True)
    p.add_argument('--owner',required=True);p.add_argument('--slug',required=True);p.add_argument('--shape',default='CPU',choices=['CPU']+sorted(GPU_SHAPES))
    p.add_argument('--competition');p.add_argument('--rules-url');p.add_argument('--quota-multiplier',type=float,default=2.)
    p.add_argument('--minutes',type=float,default=5);p.add_argument('--python-abi',default='native');p.add_argument('--expected-packages',default='{}')
    p.add_argument('--wheelhouse-manifest',type=Path);p.add_argument('--wheelhouse-dataset-ref')
    a=p.parse_args();assert not a.source.exists(),'New versioned directory required';workload=a.workload.read_text(encoding='utf-8');compile(workload,'workload.py','exec')
    runner=(Path(__file__).resolve().parents[1]/'control_plane/offline_runtime.py').read_text(encoding='utf-8')
    spec=dict(strategy='native',python_abi=a.python_abi,expected_packages=json.loads(a.expected_packages),workload_sha256=hashlib.sha256(workload.encode()).hexdigest())
    if a.wheelhouse_manifest:
        assert a.wheelhouse_dataset_ref and a.python_abi!='native','Wheelhouse needs an attached dataset and locked ABI'
        manifest=json.loads(a.wheelhouse_manifest.read_text(encoding='utf-8'))
        assert 'requirements.lock' in manifest['files'],'Manifest must lock requirements and all wheels'
        spec.update(strategy='wheelhouse',wheelhouse_manifest=a.wheelhouse_manifest.name,
            wheelhouse_manifest_sha256=hashlib.sha256(a.wheelhouse_manifest.read_bytes()).hexdigest(),wheelhouse_dataset_ref=a.wheelhouse_dataset_ref)
    code='OFFLINE_SPEC = '+repr(spec)+'\nWORKLOAD_CODE = '+repr(workload)+'\nOFFLINE_RUNNER = '+repr(runner)+"\nns={}\nexec(compile(OFFLINE_RUNNER,'kcp_offline_runtime','exec'),ns)\nns['run'](OFFLINE_SPEC,WORKLOAD_CODE)\n"
    a.source.mkdir(parents=True);(a.source/'run.py').write_text(code,encoding='utf-8',newline='\n')
    meta=dict(id=a.owner+'/'+a.slug,title=a.slug,code_file='run.py',language='python',kernel_type='script',is_private=True,enable_gpu=a.shape!='CPU',enable_internet=False,dataset_sources=[a.wheelhouse_dataset_ref] if a.wheelhouse_manifest else [],competition_sources=[a.competition] if a.competition else [],kernel_sources=[])
    profile=dict(schema=1,mode='competition_offline' if a.competition else 'offline',machine_shape=a.shape,entrypoint_sha256=hashlib.sha256(code.encode()).hexdigest(),runtime=spec)
    if a.competition:profile.update(competition_slug=a.competition,purpose='competition_workload',rules_url=a.rules_url,quota_multiplier=a.quota_multiplier,estimated_session_minutes=a.minutes)
    (a.source/'kernel-metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
    (a.source/'offline-profile.json').write_text(json.dumps(profile,indent=2)+'\n')
    print(json.dumps(validate_profile(a.source,a.shape)))
if __name__=='__main__':main()

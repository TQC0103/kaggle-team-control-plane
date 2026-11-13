"""List already-entered competitions for enabled accounts, never join anything."""
import argparse,json
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen
from concurrent.futures import ThreadPoolExecutor
from .main import DesktopRuntime
from control_plane.adapters import KaggleCliAdapter
from control_plane.credentials import EnvCredentialVault
from control_plane.scheduler import JobScheduler
from control_plane.competition_access import entered_competitions
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    with urlopen('http://127.0.0.1:8765/api/accounts',timeout=15) as f:accounts={r['id']:r for r in json.load(f)['accounts'] if r['state']=='enabled'}
    runtime=DesktopRuntime();runtime.load_credentials();vault=EnvCredentialVault(auth_refresh=runtime._auth_refresh)
    service=SimpleNamespace(database=SimpleNamespace(get_account=lambda i:accounts[i]),data_dir=runtime.data_root/'runtime',vault=vault,
        adapter=KaggleCliAdapter(auth_env_refresh=vault.refresh_subprocess_env),
        scheduler=SimpleNamespace(_redact=lambda value,env,**kwargs:JobScheduler._redact(value,env,**kwargs)))
    def inspect(aid):
        try:return entered_competitions(service,aid)
        except Exception as e:return dict(account_id=aid,status='UNAVAILABLE',error=str(e)[:700])
    with ThreadPoolExecutor(max_workers=3) as pool:rows=list(pool.map(inspect,accounts))
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(dict(accounts=rows,joined=0,accepted_rules=0,new_jobs=0),indent=2)+'\n')
    print(json.dumps(rows),flush=True)
if __name__=='__main__':main()

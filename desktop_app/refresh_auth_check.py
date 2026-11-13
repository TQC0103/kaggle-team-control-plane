"""Bounded KCP maintenance action: try app OAuth refresh, output safe outcomes.

No credential-inspection endpoint, credential export, browser login or scheduler
is used. Only the normal desktop refresh method handles stored auth material.
"""
import json
import argparse
from pathlib import Path
import tempfile
import threading
from urllib.request import urlopen
from control_plane.adapters import KaggleCliAdapter
from control_plane.credentials import EnvCredentialVault
from .main import DesktopRuntime

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', help='Refresh exactly one account even before quota sync reports an error')
    args = parser.parse_args()
    with urlopen('http://127.0.0.1:8765/api/accounts',timeout=10) as response:
        accounts=json.load(response)['accounts']
    runtime=DesktopRuntime()  # No start(): no scheduler/API/app restart.
    for account in accounts:
        if args.account_id:
            if account['id'] != args.account_id:continue
        elif not account.get('official_quota',{}).get('sync_error'):continue
        result={'account_id':account['id'],'account':account['kaggle_username'],
                'oauth_refresh_completed':False,'auth_verified_after_refresh':False}
        try:
            completed=runtime._refresh_oauth_credential(account['credential_env_ref'])
            result['oauth_refresh_completed']=completed
            if not completed:
                result['outcome']='NO_STORED_OAUTH_REFRESH_BUNDLE'
            else:
                # Auth stays entirely in the Control Plane subprocess boundary.
                with tempfile.TemporaryDirectory(prefix='kcp-refresh-check-') as directory:
                    vault=EnvCredentialVault(auth_refresh=runtime._auth_refresh)
                    env=vault.build_subprocess_env(account['credential_env_ref'],account['kaggle_username'],Path(directory))
                    KaggleCliAdapter(auth_env_refresh=vault.refresh_subprocess_env)._run(['quota','--format','json'],env,threading.Event(),timeout_seconds=25)
                    result['auth_verified_after_refresh']=True
                    result['outcome']='REFRESH_WORKS_RESTART_CAN_LOAD_RENEWED_AUTH'
                    del env
        except Exception as exc:
            # Never expose exception text: SDK/CLI errors could include auth.
            result['outcome']='REFRESH_OR_AUTH_FAILED'
            result['error_type']=type(exc).__name__
        print(json.dumps(result),flush=True)

if __name__=='__main__':main()

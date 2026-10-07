from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import unittest
from unittest.mock import patch

from control_plane.adapters import KaggleCliAdapter, AdapterError
from control_plane.credentials import EnvCredentialVault
from control_plane.errors import ValidationError
from desktop_app.auth_refresh import DesktopAuthRefresh
from desktop_app import oauth


REF = "KCP_TEST_ACCOUNT"

def bundle(token="fixture-old"):
    return dict(kind=oauth.OAUTH_KIND, username="fixture-member", refresh_token="fixture-refresh",
                access_token=token, access_token_expiration="2099-01-01T00:00:00+00:00", scopes=oauth.OAUTH_SCOPES)

class Store:
    def __init__(self, value=None):
        self.values={REF:json.dumps(bundle()) if value is None else value}
        self.saves=0
    def list_refs(self):return list(self.values)
    def load(self, ref):return self.values[ref]
    def save(self, ref, value):self.values[ref]=value;self.saves+=1
    def forget(self, ref):return self.values.pop(ref,None) is not None

class RefreshTests(unittest.TestCase):
    def manager(self, resolver=None, store=None):
        self.now=0.;self.env={};self.store=store or Store();self.calls=[]
        def resolve(b, *, force=False):
            self.calls.append(force)
            b=deepcopy(b)
            if force:b['access_token']='fixture-new'
            return b['access_token'],b
        self.m=DesktopAuthRefresh(self.store,environ=self.env,resolver=resolver or resolve,clock=lambda:self.now)
        return self.m

    def test_long_lived_worker_reloads_token_without_restart(self):
        m=self.manager();v=EnvCredentialVault(self.env,auth_refresh=m)
        env=v.build_subprocess_env(REF,'fixture-member','./fixture-config')
        self.assertEqual(env['KAGGLE_API_TOKEN'],'fixture-old')
        self.now=13*3600
        self.store.values[REF]=json.dumps(bundle('fixture-renewed'))
        v.refresh_subprocess_env(env)
        self.assertEqual(env['KAGGLE_API_TOKEN'],'fixture-renewed')
        self.assertEqual(env['KAGGLE_USERNAME'],'fixture-member')
        self.assertTrue(env['KAGGLE_CONFIG_DIR'].endswith('fixture-config'))

    def test_concurrent_stale_workers_force_one_refresh(self):
        m=self.manager();m.ensure(REF)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _:m.ensure(REF,force=True,observed_token='fixture-old'),range(16)))
        self.assertEqual(self.calls.count(True),1)
        self.assertEqual(self.env[REF],'fixture-new')

    def test_refresh_failure_is_sanitized_and_backed_off(self):
        calls=[]
        def fail(*a,**kw):calls.append(1);raise TimeoutError('fixture-secret-token-in-error')
        m=self.manager(resolver=fail)
        for _ in range(3):
            with self.assertRaises(ValidationError) as caught:m.ensure(REF)
            self.assertNotIn('fixture-secret',str(caught.exception))
        self.assertEqual(len(calls),1)
        self.assertEqual(m.status(REF)['state'],'temporarily_unavailable')
        self.now=31
        with self.assertRaises(ValidationError):m.ensure(REF)
        self.assertEqual(len(calls),2)

    def test_revoked_refresh_requires_reconnect_and_login_clears_backoff(self):
        def fail(*a,**kw):raise RuntimeError('invalid_grant fixture-secret')
        m=self.manager(resolver=fail)
        with self.assertRaises(ValidationError) as caught:m.ensure(REF)
        self.assertIn('reconnect',str(caught.exception))
        self.assertNotIn('fixture-secret',str(caught.exception))
        self.assertEqual(m.status(REF)['state'],'relogin_required')
        m.save(REF,'fixture-static-token')
        self.assertFalse(m.ensure(REF))
        self.assertEqual(self.env[REF],'fixture-static-token')

    def test_force_refresh_respects_failure_backoff(self):
        calls=[]
        def fail(*a,**kw):calls.append(1);raise RuntimeError('invalid_grant')
        m=self.manager(resolver=fail)
        for _ in range(4):
            with self.assertRaises(ValidationError):m.ensure(REF,force=True)
        self.assertEqual(len(calls),1)
        m.invalidate(REF)
        with self.assertRaises(ValidationError):m.ensure(REF,force=True)
        self.assertEqual(len(calls),2)

    def test_static_credential_does_not_use_oauth(self):
        m=self.manager(store=Store('fixture-static-token'))
        self.assertFalse(m.ensure(REF));self.assertEqual(self.calls,[])
        self.assertEqual(self.env[REF],'fixture-static-token')

    def test_forget_prevents_stale_token_reuse(self):
        m=self.manager();m.ensure(REF);self.assertTrue(m.forget(REF))
        self.assertNotIn(REF,self.env)
        with self.assertRaises(ValidationError):m.ensure(REF)

    def test_periodic_refresh_rechecks_and_stops(self):
        m=self.manager();m.start(interval=.01)
        deadline=time.monotonic()+1
        while not self.calls and time.monotonic()<deadline:time.sleep(.005)
        self.now=61
        deadline=time.monotonic()+1
        while len(self.calls)<2 and time.monotonic()<deadline:time.sleep(.005)
        m.stop();self.assertGreaterEqual(len(self.calls),2)
        self.assertFalse(m._thread.is_alive())

    def test_status_does_not_wait_for_slow_refresh(self):
        m=self.manager();locked=threading.Event();release=threading.Event()
        def hold():
            with m._lock(REF):locked.set();release.wait(1)
        t=threading.Thread(target=hold);t.start();locked.wait(1)
        before=time.monotonic();m.status(REF)
        self.assertLess(time.monotonic()-before,.1);release.set();t.join()

class AdapterRefreshTests(unittest.TestCase):
    def setUp(self):
        self.event=threading.Event();self.env={};self.refresh=[]
        self.adapter=KaggleCliAdapter(auth_env_refresh=lambda env,force:self.refresh.append(force))

    def test_auth_error_read_retries_once(self):
        with patch.object(self.adapter,'_run_once',side_effect=[AdapterError('Authentication required'),'ok']) as run:
            self.assertEqual(self.adapter._run(['quota'],self.env,self.event),'ok')
            self.assertEqual(run.call_count,2)
        self.assertEqual(self.refresh,[False,True])

    def test_repeated_auth_error_is_bounded(self):
        with patch.object(self.adapter,'_run_once',side_effect=AdapterError('401 Unauthorized')) as run:
            with self.assertRaises(AdapterError):self.adapter._run(['kernels','status','member/slug'],self.env,self.event)
            self.assertEqual(run.call_count,2)

    def test_mutation_is_never_replayed(self):
        for args in [['kernels','push','-p','fixture'],['datasets','create','-p','fixture'],['datasets','metadata','member/data','--update']]:
            self.refresh.clear()
            with patch.object(self.adapter,'_run_once',side_effect=AdapterError('Authentication required')) as run:
                with self.assertRaises(AdapterError):self.adapter._run(args,self.env,self.event)
                self.assertEqual(run.call_count,1)
            self.assertEqual(self.refresh,[False])

    def test_permissions_error_is_not_treated_as_expiry(self):
        with patch.object(self.adapter,'_run_once',side_effect=AdapterError('403 Forbidden')) as run:
            with self.assertRaises(AdapterError):self.adapter._run(['quota'],self.env,self.event)
            self.assertEqual(run.call_count,1)

    def test_live_logs_get_new_auth_and_retry_once(self):
        with patch.object(self.adapter,'_run_follow_snapshot_once',side_effect=[AdapterError('Authentication required'),'stream']) as run:
            self.assertEqual(self.adapter._run_follow_snapshot(['kernels','logs','--follow','member/slug'],self.env,self.event),'stream')
            self.assertEqual(run.call_count,2)
        self.assertEqual(self.refresh,[False,True])

    def test_static_token_failure_has_no_futile_retry(self):
        self.adapter.auth_env_refresh=lambda *args:False
        with patch.object(self.adapter,'_run_once',side_effect=AdapterError('Authentication required')) as run:
            with self.assertRaises(AdapterError):self.adapter._run(['quota'],self.env,self.event)
            self.assertEqual(run.call_count,1)

class ExpiryTests(unittest.TestCase):
    def test_near_expiry_and_forced_token_are_renewed(self):
        class Client:
            def __init__(self,*a,**k):pass
            def __enter__(self):return self
            def __exit__(self,*a):return False
        seen=[]
        class Credentials:
            def __init__(self,**kw):
                seen.append(kw);self._refresh_token=kw['refresh_token'];self._access_token='fixture-renewed'
                self._access_token_expiration=datetime.now(timezone.utc)+timedelta(hours=12)
                self._scopes=kw['scopes']
            def get_access_token(self):return self._access_token
            def get_username(self):return 'fixture-member'
        with patch.object(oauth,'KaggleClient',Client),patch.object(oauth,'InMemoryKaggleCredentials',Credentials):
            b=bundle();b['access_token_expiration']=(datetime.now(timezone.utc)+timedelta(minutes=4)).isoformat()
            self.assertEqual(oauth.resolve_oauth_access_token(b)[0],'fixture-renewed')
            self.assertEqual(seen[-1]['access_token'],'')
            oauth.resolve_oauth_access_token(bundle(),force=True)
            self.assertEqual(seen[-1]['access_token'],'')

if __name__=='__main__':unittest.main()

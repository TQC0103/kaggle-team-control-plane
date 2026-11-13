import copy
import unittest

from cloud_orchestrator.cpu_fixture import proposal, receipt
from cloud_orchestrator.coordinator import cycle
from cloud_orchestrator.protocol import digest


def plan():
    return dict(schema=1, kind='cpu_fixture', id='cpu-fixture-'+'a'*12, cap=2,
        account_id='acct_d321057bf0954d048b448711e0efed7f', username='tqc0103',
        created_at=1000, expires_at=6400)


class Store:
    def __init__(self):
        p = plan()
        self.doc = dict(plan=p, plan_sha256=digest(p), state={}, used=0)
        self.outbox, self.reserved, self.errors, self.events = {}, [], [], []
        self.fence, self.enabled, self.claimed = 0, True, False
    def claim(self, owner, now):
        if self.claimed:return None
        self.claimed, self.owner = True, owner
        self.fence += 1
        return self.fence
    def programs(self, limit):return [copy.deepcopy(self.doc)]
    def intent(self, key):return self.outbox.get(key)
    def reserve(self, identity, owner, fence, account, job, key, sha, cap):
        if not self.enabled or len(self.reserved)>=2 or self.doc['used']>=cap:return False
        if owner!=self.owner or fence!=self.fence:return False
        self.events.append('INTENT')
        self.outbox[key] = dict(status='INTENT')
        self.reserved.append(job)
        self.doc['used'] += 1
        return True
    def accepted(self, key, job):self.outbox[key].update(status='ACCEPTED', receipt=job)
    def receipt(self, key, job):self.outbox[key].update(status='REMOTE_VERIFIED', receipt=job)
    def save(self, identity, owner, fence, state):
        if owner!=self.owner or fence!=self.fence:raise ValueError('stale fence')
        self.doc['state']=copy.deepcopy(state)
    def release_slot(self, account, job):self.reserved=[j for j in self.reserved if j!=job]
    def error(self, identity, kind):self.errors.append(kind)
    def finish(self, owner, fence):
        if self.owner==owner and self.fence==fence:self.claimed=False


class Transport:
    def __init__(self, store):
        self.store, self.posts, self.remote = store, [], {}
        self.lose_response, self.bad_output, self.lost_after_accept = False, False, False
    def submit_once(self, p):
        assert self.store.events[-1]=='INTENT', 'intent required before external POST'
        self.store.events.append('POST')
        self.posts.append(p)
        row=dict(id=p['id'],account_id=p['account_id'],kernel_slug=p['kernel_slug'],
                 source_sha256=p['source_sha256'],version=1,kernel_id=len(self.posts),status='running')
        self.remote[p['id']]=row
        if self.lose_response:raise TimeoutError('SENSITIVE_SDK_MESSAGE')
        return {k:v for k,v in row.items() if k not in {'id','status'}}
    def observe(self, row):
        if self.lost_after_accept:
            self.lost_after_accept=False
            raise TimeoutError('SENSITIVE_AFTER_ACCEPT')
        return copy.deepcopy(self.remote[row['id']])
    def reconcile(self, p):return copy.deepcopy(self.remote[p['id']])
    def read_json(self, row, basename):
        if self.bad_output:return {'counter':99}
        step=1 if row['id']==proposal(plan(),1)['id'] else 2
        return receipt(plan(),step)


class CoordinatorTests(unittest.TestCase):
    def test_two_cpu_jobs_only_after_exact_parent_completion_and_output(self):
        s=Store();t=Transport(s)
        cycle(s,t,now=1000)
        self.assertEqual(len(t.posts),1)
        self.assertEqual(t.posts[0]['accelerator'],'cpu')
        cycle(s,t,now=1001)
        self.assertEqual(len(t.posts),1)
        t.remote[proposal(plan(),1)['id']]['status']='complete'
        cycle(s,t,now=1002)
        self.assertEqual(s.doc['state']['status'],'PARENT_VALIDATED')
        cycle(s,t,now=1003)
        self.assertEqual(len(t.posts),2)
        self.assertEqual(t.posts[1]['kernel_sources'],[t.posts[0]['kernel_slug']])
        t.remote[proposal(plan(),2)['id']]['status']='complete'
        cycle(s,t,now=1004)
        self.assertEqual(s.doc['state']['status'],'DONE')
        cycle(s,t,now=1005)
        self.assertEqual(len(t.posts),2)
        self.assertEqual(s.reserved,[])

    def test_lost_mutation_response_reconciles_without_second_post(self):
        s=Store();t=Transport(s);t.lose_response=True
        cycle(s,t,now=1000)
        self.assertEqual(len(t.posts),1)
        self.assertNotIn('SENSITIVE_SDK_MESSAGE',str(s.errors))
        cycle(s,t,now=1001)
        self.assertEqual(len(t.posts),1)
        self.assertEqual(s.doc['state']['status'],'RUNNING')

    def test_accepted_receipt_survives_status_timeout(self):
        s=Store();t=Transport(s);t.lost_after_accept=True
        cycle(s,t,now=1000)
        self.assertEqual(next(iter(s.outbox.values()))['status'],'ACCEPTED')
        cycle(s,t,now=1001)
        self.assertEqual(len(t.posts),1)

    def test_wrong_parent_output_never_opens_child(self):
        s=Store();t=Transport(s);cycle(s,t,now=1000)
        t.remote[proposal(plan(),1)['id']]['status']='complete';t.bad_output=True
        cycle(s,t,now=1001);cycle(s,t,now=1002)
        self.assertEqual(len(t.posts),1)

    def test_no_slot_or_disabled_account_never_submits(self):
        for full in [True,False]:
            s=Store();t=Transport(s)
            if full:s.reserved=['one','two']
            else:s.enabled=False
            cycle(s,t,now=1000)
            self.assertEqual(t.posts,[])

    def test_modified_plan_or_non_cpu_kind_never_submits(self):
        for tamper in [dict(cap=3),dict(kind='gpu'),dict(account_id='another')]:
            s=Store();s.doc['plan'].update(tamper);t=Transport(s)
            cycle(s,t,now=1000)
            self.assertEqual(t.posts,[])

    def test_expired_plan_never_submits(self):
        s=Store();t=Transport(s);cycle(s,t,now=7000)
        self.assertEqual(t.posts,[])
        self.assertEqual(s.doc['state']['status'],'EXPIRED')

    def test_cap_exhausted_never_submits(self):
        s=Store();s.doc['used']=2;t=Transport(s);cycle(s,t,now=1000)
        self.assertEqual(t.posts,[])

    def test_second_active_tick_cannot_claim_or_submit(self):
        s=Store();s.claimed=True;t=Transport(s);r=cycle(s,t,now=1000)
        self.assertEqual(r['status'],'SKIP_LEASE')
        self.assertEqual(t.posts,[])

    def test_both_sources_are_private_offline_cpu_with_ten_minute_limits(self):
        for step in [1,2]:
            p=proposal(plan(),step)
            self.assertTrue(p['is_private'])
            self.assertFalse(p['enable_internet'])
            self.assertEqual(p['timeout_seconds'],600)
            compile(p['source'],'fixture.py','exec')


if __name__=='__main__':unittest.main()

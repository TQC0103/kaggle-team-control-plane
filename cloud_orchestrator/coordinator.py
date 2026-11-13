"""Finite CPU fixture coordinator with durable outbox and fenced account slots.

Current release cannot dispatch scientific jobs. Infrastructure fixture cap2.
"""
from __future__ import annotations

from copy import deepcopy
import re
import time
from uuid import uuid4

from .cpu_fixture import proposal, receipt, validate_plan
from .protocol import digest


def cycle(store, transport, now=None):
    now = time.time() if now is None else now
    owner = uuid4().hex
    fence = store.claim(owner, now)
    if fence is None:
        return dict(status='SKIP_LEASE', scientific_submission_enabled=False)
    outcomes = {}
    for document in store.programs(4):
        identity = document.get('_document_id', document.get('plan', {}).get('id', 'invalid'))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(identity)):
            identity = 'invalid'
        try:
            plan, state = document['plan'], deepcopy(document.get('state', {}))
            validate_plan(plan)
            if identity != plan['id']:
                raise ValueError('program document identity mismatch')
            if document.get('plan_sha256') != digest(plan):
                raise ValueError('sealed CPU fixture plan mismatch')
            if state.get('status') in {'DONE', 'STOP', 'EXPIRED'}:
                outcomes[identity] = state['status']
                continue
            if now >= plan['expires_at']:
                state.update(status='EXPIRED')
                store.save(identity, owner, fence, state)
                outcomes[identity] = 'EXPIRED'
                continue
            step = state.get('step', 1)
            if step not in {1, 2}:
                raise ValueError('invalid CPU fixture step')
            p = proposal(plan, step)
            job = state.get('job')
            if job:
                observed = transport.observe(job)
                if observed['status'] in {'failed', 'cancelled'}:
                    state.update(status='STOP', reason='REMOTE_TERMINAL_FAILURE')
                elif observed['status'] == 'complete':
                    result = transport.read_json(job, 'echo_receipt.json')
                    if result != receipt(plan, step):
                        raise ValueError('exact parent output mismatch')
                    store.release_slot(p['account_id'], p['id'])
                    state.setdefault('completed', []).append(dict(job=job, receipt=result))
                    if step == 2:
                        state.update(status='DONE')
                    else:
                        state.update(status='PARENT_VALIDATED', step=2, job=None)
                store.save(identity, owner, fence, state)
                outcomes[identity] = state.get('status', observed['status'])
                continue
            key = digest(dict(program=identity, proposal=p['id'], source=p['source_sha256']))
            intent = store.intent(key)
            if intent:
                # The request may have committed even if its response was lost.
                observed = transport.reconcile(p)
                if observed['status'] not in {'queued', 'running', 'complete'}:
                    raise ValueError('uncertain mutation requires review')
                job = {k: observed[k] for k in ['account_id', 'kernel_slug', 'version', 'kernel_id', 'source_sha256']}
                job['id'] = p['id']
            else:
                if not store.reserve(identity, owner, fence, p['account_id'], p['id'], key,
                                     p['source_sha256'], plan['cap']):
                    outcomes[identity] = 'WAIT_ACCOUNT_OR_BUDGET'
                    continue
                # Persisted intent + reservation exist before this one POST.
                saved = transport.submit_once(p)
                job = dict(id=p['id'], **saved)
                store.accepted(key, job)
                observed = transport.observe(job)
                if observed['status'] not in {'queued', 'running', 'complete'}:
                    raise ValueError('new remote job not verified')
            store.receipt(key, job)
            state.update(status='RUNNING', job=job)
            store.save(identity, owner, fence, state)
            outcomes[identity] = 'REMOTE_VERIFIED'
        except Exception as exc:
            # Never dump SDK errors, auth, source or output download URLs.
            store.error(identity, type(exc).__name__)
            outcomes[identity] = 'RECONCILE_NO_REPLAY'
    store.finish(owner, fence)
    return dict(status='TICK_COMPLETE', programs=outcomes, scientific_submission_enabled=False)


class FirestoreCoordinatorStore:
    def __init__(self, project, prefix='kcp_cloud'):
        from google.cloud import firestore
        self.fs = firestore
        self.db = firestore.Client(project=project, database='(default)')
        if prefix not in {'kcp_cloud','kcp_fm'}:
            raise ValueError('unsupported coordinator namespace')
        self.prefix=prefix
        self.control = self.db.collection(prefix+'_coordinator').document('control')

    def claim(self, owner, now):
        @self.fs.transactional
        def run(tx):
            c = self.control.get(transaction=tx, retry=None, timeout=8).to_dict() or {}
            if c.get('lease_until', 0) > now:
                return None
            fence = c.get('fence', 0)+1
            tx.set(self.control, dict(owner=owner, fence=fence, lease_until=now+90))
            return fence
        return run(self.db.transaction(max_attempts=1))

    def programs(self, limit):
        return [dict(r.to_dict(), _document_id=r.id) for r in self.db.collection(self.prefix+'_programs').limit(limit).stream(retry=None, timeout=8)]

    def intent(self, key):
        return self.db.collection(self.prefix+'_outbox').document(key).get(retry=None, timeout=8).to_dict()

    def reserve(self, identity, owner, fence, account, job, key, source_sha, cap):
        ref = self.db.collection(self.prefix+'_accounts').document(account)
        outbox = self.db.collection(self.prefix+'_outbox').document(key)
        program = self.db.collection(self.prefix+'_programs').document(identity)
        @self.fs.transactional
        def run(tx):
            c = self.control.get(transaction=tx, retry=None, timeout=8).to_dict() or {}
            a = ref.get(transaction=tx, retry=None, timeout=8).to_dict() or {}
            p = program.get(transaction=tx, retry=None, timeout=8).to_dict() or {}
            existing = outbox.get(transaction=tx, retry=None, timeout=8).to_dict()
            if c.get('owner') != owner or c.get('fence') != fence or c.get('lease_until', 0) <= time.time():
                return False
            if existing:
                return False
            reserved = a.get('reserved_jobs', [])
            if not a.get('enabled') or len(reserved) >= 2 or p.get('used', 0) >= cap:
                return False
            tx.set(ref, dict(a, reserved_jobs=reserved+[job]))
            tx.update(program, dict(used=p.get('used', 0)+1))
            tx.set(outbox, dict(program=identity, account_id=account, job_id=job,
                                source_sha256=source_sha, status='INTENT', fence=fence,
                                created_at=time.time()))
            return True
        return run(self.db.transaction(max_attempts=1))

    def accepted(self, key, job):
        self.receipt(key, job, verified=False)

    def receipt(self, key, job, verified=True):
        ref = self.db.collection(self.prefix+'_outbox').document(key)
        # Completion of an exact already-claimed intent remains safe even if
        # its request outlived the tick lease; never authorizes another POST.
        @self.fs.transactional
        def run(tx):
            intent = ref.get(transaction=tx, retry=None, timeout=8).to_dict()
            if (not intent or intent['source_sha256'] != job['source_sha256']
                    or intent['account_id'] != job['account_id'] or intent['job_id'] != job['id']):
                raise ValueError('receipt identity mismatch')
            tx.update(ref, dict(status='REMOTE_VERIFIED' if verified else 'REQUEST_ACCEPTED_REMOTE_UNVERIFIED', receipt=job))
        run(self.db.transaction(max_attempts=1))

    def save(self, identity, owner, fence, state):
        ref = self.db.collection(self.prefix+'_programs').document(identity)
        @self.fs.transactional
        def run(tx):
            c = self.control.get(transaction=tx, retry=None, timeout=8).to_dict() or {}
            if c.get('owner') != owner or c.get('fence') != fence or c.get('lease_until', 0) <= time.time():
                raise ValueError('lost coordinator fence')
            tx.update(ref, dict(state=state, updated_at=time.time()))
        run(self.db.transaction(max_attempts=1))

    def release_slot(self, account, job):
        ref = self.db.collection(self.prefix+'_accounts').document(account)
        @self.fs.transactional
        def run(tx):
            row = ref.get(transaction=tx, retry=None, timeout=8).to_dict() or {}
            tx.set(ref, dict(row, reserved_jobs=[j for j in row.get('reserved_jobs', []) if j != job]))
        run(self.db.transaction(max_attempts=1))

    def error(self, identity, error_class):
        self.db.collection(self.prefix+'_coordinator_events').document(identity).set(
            dict(status='RECONCILE_NO_REPLAY', error_class=error_class, at=time.time()), retry=None, timeout=8)

    def finish(self, owner, fence):
        @self.fs.transactional
        def run(tx):
            c = self.control.get(transaction=tx, retry=None, timeout=8).to_dict() or {}
            if c.get('owner') == owner and c.get('fence') == fence:
                tx.update(self.control, dict(lease_until=0, last_finished_at=time.time()))
        run(self.db.transaction(max_attempts=1))

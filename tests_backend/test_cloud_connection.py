import copy
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest

from cloud_orchestrator.connection import (
    PROJECT, ReceiptJournal, connection_plan, validate_connection_plan, connect,
)
from desktop_app.auth_refresh import DesktopAuthRefresh


def account(i):
    return dict(id='acct_'+f'{i:032x}', kaggle_username=f'fixture{i}', state='enabled')


class FakeVault:
    def __init__(self):
        self.payloads = []
        self.created = False
        self.fail = False
        self.verified = []
    def exists(self, *_):
        return self.created
    def create_verified(self, project, secret, data):
        self.created = True
        if self.fail:
            raise TimeoutError('SENSITIVE_EXCEPTION_SENTINEL')
        self.payloads.append(data)
        return f'projects/{project}/secrets/{secret}/versions/1'
    def verify_version(self, name):
        self.verified.append(name)


class Provider:
    called = 0
    mismatch = False
    def send(self, accounts, sink):
        self.called += 1
        items = [dict(id=a['id'], username=a['username'], kind='kaggle_oauth_v1',
                      auth=dict(username=a['username'], refresh_token='SECRET_REFRESH_SENTINEL')) for a in accounts]
        if self.mismatch:
            items[0]['auth']['username'] = 'anotheraccount'
        return sink(items)


class ConnectionTests(unittest.TestCase):
    def test_plan_contains_only_selected_public_metadata(self):
        a = account(1)
        a['credential_env_ref'] = 'PRIVATE_REF_NOT_TO_EXPORT'
        p = connection_plan(PROJECT, [a])
        self.assertNotIn('PRIVATE_REF_NOT_TO_EXPORT', json.dumps(p))
        self.assertEqual(len(p['vaults']), 1)
        validate_connection_plan(p)

    def test_eight_accounts_fit_two_vaults_latest_two_versions(self):
        p = connection_plan(PROJECT, [account(i) for i in range(1, 9)])
        self.assertEqual([len(v['accounts']) for v in p['vaults']], [4, 4])
        self.assertEqual(len(p['vaults'])*p['retention_max_versions'], 4)

    def test_reordered_input_same_plan(self):
        rows = [account(i) for i in range(1, 5)]
        self.assertEqual(connection_plan(PROJECT, rows), connection_plan(PROJECT, rows[::-1]))

    def test_modified_plan_rejected(self):
        p = connection_plan(PROJECT, [account(1)])
        p['submission_enabled'] = True
        with self.assertRaises(ValueError): validate_connection_plan(p)

    def test_wrong_project_disabled_duplicate_unknown_rejected(self):
        for project, rows in [('another-project', [account(1)]),
            (PROJECT, [dict(account(1), state='disabled')]),
            (PROJECT, [account(1), account(1)]), (PROJECT, []),
            (PROJECT, [account(i) for i in range(9)])]:
            with self.subTest(project=project, rows=len(rows)), self.assertRaises(ValueError):
                connection_plan(project, rows)

    def test_enrollment_only_receipts_no_secret_stdout_or_files(self):
        p, vault, provider = connection_plan(PROJECT, [account(1)]), FakeVault(), Provider()
        with tempfile.TemporaryDirectory() as td:
            journal = ReceiptJournal(Path(td)/'journal.json')
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                result = connect(p, provider, vault, journal)
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(result['status'], 'CONNECTED')
            self.assertFalse(result['submission_enabled'])
            self.assertNotIn('SECRET_REFRESH_SENTINEL', str(result))
            self.assertNotIn('SECRET_REFRESH_SENTINEL', journal.path.read_text())
            self.assertIn(b'SECRET_REFRESH_SENTINEL', vault.payloads[0])

    def test_wrong_identity_never_creates_secret(self):
        p, vault, provider = connection_plan(PROJECT, [account(1)]), FakeVault(), Provider()
        provider.mismatch = True
        with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
            connect(p, provider, vault, ReceiptJournal(Path(td)/'journal.json'))
        self.assertFalse(vault.created)

    def test_existing_vault_never_loads_desktop_credentials(self):
        vault, provider = FakeVault(), Provider()
        vault.created = True
        with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
            connect(connection_plan(PROJECT, [account(1)]), provider, vault, ReceiptJournal(Path(td)/'j.json'))
        self.assertEqual(provider.called, 0)

    def test_timeout_cannot_be_replayed_even_with_new_provider(self):
        vault, provider = FakeVault(), Provider()
        vault.fail = True
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'j.json'
            with self.assertRaises(TimeoutError):
                connect(connection_plan(PROJECT, [account(1)]), provider, vault, ReceiptJournal(path))
            self.assertNotIn('SENSITIVE_EXCEPTION_SENTINEL', path.read_text())
            other = Provider()
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                connect(connection_plan(PROJECT, [account(1)]), other, vault, ReceiptJournal(path))
            self.assertEqual(other.called, 0)

    def test_verified_resume_checks_remote_version_without_creds(self):
        vault, provider = FakeVault(), Provider()
        plan = connection_plan(PROJECT, [account(1)])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'j.json'
            connect(plan, provider, vault, ReceiptJournal(path))
            other = Provider()
            connect(plan, other, vault, ReceiptJournal(path))
            self.assertEqual(other.called, 0)
            self.assertEqual(len(vault.verified), 1)

    def test_corrupt_receipt_cannot_be_echoed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'j.json'
            path.write_text(json.dumps({'wrong': {'auth': 'SECRET'}}))
            with self.assertRaises(ValueError): ReceiptJournal(path)


class SnapshotTests(unittest.TestCase):
    def manager(self, stored):
        class Store:
            def __init__(self): self.refs = []
            def load(self, ref): self.refs.append(ref); return stored
            def save(self, *_): raise AssertionError('snapshot must never save/refresh')
        store = Store()
        env = {}
        manager = DesktopAuthRefresh(store, environ=env,
            resolver=lambda *_: self.fail('snapshot must not refresh'))
        return manager, store, env

    def test_only_selected_ref_no_desktop_save_refresh_or_env_mutation(self):
        manager, store, env = self.manager(json.dumps(dict(kind='kaggle_oauth_v1',
            username='fixture1', refresh_token='FAKE_REFRESH')))
        observed = []
        def sink(items): observed.extend(copy.deepcopy(items)); return {'status': 'VERIFIED'}
        result = manager.send_cloud_snapshot([dict(id=account(1)['id'], username='fixture1', ref='KCP_FIXTURE')], sink)
        self.assertEqual(result, {'status': 'VERIFIED'})
        self.assertEqual(store.refs, ['KCP_FIXTURE'])
        self.assertEqual(env, {})
        self.assertEqual(observed[0]['auth']['refresh_token'], 'FAKE_REFRESH')

    def test_oauth_wrong_username_never_reaches_sink(self):
        manager, _, _ = self.manager(json.dumps(dict(kind='kaggle_oauth_v1',
            username='another', refresh_token='FAKE')))
        with self.assertRaises(ValueError):
            manager.send_cloud_snapshot([dict(id=account(1)['id'], username='fixture1', ref='KCP_FIXTURE')],
                                        lambda _: self.fail('wrong account transfer'))

    def test_static_binding_mismatch_and_unsupported_keys_blocked(self):
        for stored in [{'username': 'another', 'key': 'FAKE'}, {'key': 'FAKE', 'path': '/private'}]:
            manager, _, _ = self.manager(json.dumps(stored))
            with self.assertRaises(ValueError):
                manager.send_cloud_snapshot([dict(id=account(1)['id'], username='fixture1', ref='KCP_FIXTURE')],
                                            lambda _: self.fail('bad static transfer'))


if __name__ == '__main__':
    unittest.main()

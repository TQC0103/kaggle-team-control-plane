"""Owned remote dataset inventory and explicit, verified deletion. No credentials here."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .dataset_transfer import _account_cli, _REF
from .datasets import _now
from .errors import ConflictError, NotFoundError, ValidationError

_ACTIVE = {'queued', 'running'}
_JOB_TERMINAL = {'succeeded', 'failed', 'cancelled'}


def _csv_page(text):
    lines = text.splitlines()
    start = next((i for i, s in enumerate(lines) if s.startswith('ref,')), None)
    token = re.search(r'^Next Page Token = (.+)$', text, re.M)
    if start is None:
        if 'No datasets found' in text:
            return [], None
        raise ValidationError('Kaggle did not return an owned dataset list')
    return list(csv.DictReader(io.StringIO('\n'.join(lines[start:])))), token.group(1).strip() if token else None


class DatasetLibrary:
    def __init__(self, service, cli_provider=None):
        self.service = service
        self._cli_provider = cli_provider or (lambda aid: _account_cli(service, aid, max_text_chars=128000))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.path = service.data_dir / 'dataset-library.json'
        self._data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'rows': {}, 'accounts': {}, 'deletions': {}, 'verifications': {},
        }
        self._refreshing = False
        self._threads = []
        for op in self._data['deletions'].values():
            if op['status'] in _ACTIVE:
                op.update(status='uncertain', phase='App restarted; check remote state. Delete was not replayed.')
        self._save()

    def close(self):
        self._stop.set()
        deadline = time.monotonic() + 2
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))

    def _save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def _owner(self, account_id, ref):
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise ValidationError('Dataset reference must be owner/slug')
        account = self.service.database.get_account(account_id)
        if account['state'] != 'enabled' or not account.get('credential_env_ref'):
            raise ValidationError('Choose an enabled authenticated owner account')
        if ref.split('/')[0].casefold() != account['kaggle_username'].casefold():
            raise ValidationError('Only the owning account can delete or register this dataset')
        return account

    def _dependency_map(self):
        refs = {}
        for job in self.service.database.list_jobs():
            if job['status'] in _JOB_TERMINAL and not job.get('remote_may_be_running'):
                continue
            sources = job.get('metadata', {}).get('dataset_sources', [])
            source = Path(job['source_dir']).resolve()
            if not source.is_relative_to(self.service.allowed_source_root):
                refs.setdefault('*', []).append({'job_id': job['id'], 'reason': 'Active job input location cannot be checked'})
                continue
            if source.is_relative_to(self.service.allowed_source_root):
                metadata = source / 'kernel-metadata.json'
                try:
                    if metadata.stat().st_size <= 1024**2:
                        sources = sources + json.loads(metadata.read_text(encoding='utf-8-sig')).get('dataset_sources', [])
                except (OSError, ValueError):
                    # Unknown active inputs must not silently permit deletion.
                    refs.setdefault('*', []).append({'job_id': job['id'], 'reason': 'Active job inputs cannot be checked'})
                    continue
            for source_ref in sources:
                ref = str(source_ref).split('/versions/')[0].casefold()
                refs.setdefault(ref, []).append({'job_id': job['id'], 'reason': 'Active job uses this dataset'})
        return refs

    def _dependencies(self, ref, refs=None):
        refs = self._dependency_map() if refs is None else refs
        return refs.get(ref.casefold(), []) + refs.get('*', [])

    def assert_job_inputs_available(self, specs):
        for spec in specs:
            source = Path(spec['source_dir']) / 'kernel-metadata.json'
            if not source.is_file() or source.stat().st_size > 1024**2:
                continue  # Normal source validation handles malformed bundles.
            try:
                refs = json.loads(source.read_text(encoding='utf-8-sig')).get('dataset_sources', [])
            except (ValueError, OSError):
                continue
            for ref in refs:
                bare = str(ref).split('/versions/')[0]
                if any(op['dataset_ref'].casefold() == bare.casefold() and
                       (op['status'] in {'queued', 'running', 'uncertain'} or
                        (op['status'] == 'deleted' and self._data['rows'].get(op['dataset_ref'], {}).get('remote_state') == 'deleted'))
                       for op in self._data['deletions'].values()):
                    raise ConflictError('Job input is being deleted, unresolved or confirmed deleted on Kaggle')

    def blocks_mutation(self, account_id, ref):
        with self._lock:
            return any((r['account_id'] == account_id and r['status'] in _ACTIVE)
                       or (r['dataset_ref'].casefold() == ref.casefold() and r['status'] == 'uncertain')
                       for r in self._data['deletions'].values())

    def snapshot(self):
        with self._lock:
            rows = {k: dict(v) for k, v in self._data['rows'].items()}
            verifications = dict(self._data['verifications'])
            accounts = dict(self._data['accounts'])
            deletions = [dict(v) for v in self._data['deletions'].values()]
            refreshing = self._refreshing
        for op in reversed(self.service.datasets.list_operations()):
            if op.get('kind') != 'upload':
                continue
            ref = op['dataset_ref']
            row = rows.setdefault(ref, {'dataset_ref': ref, 'account_id': op['account_id'], 'owner': ref.split('/')[0],
                'title': op['title'], 'bytes': op.get('bytes'), 'last_updated': None, 'is_private': None,
                'remote_state': 'not_synced', 'synced_at': None})
            row['upload_status'] = op['status']
            row['file_count'] = op.get('file_count')
            row['upload_operation_id'] = op['id']
            if op.get('visibility_verified'):
                row['is_private'] = op['is_private']
            if op['status'] not in {'succeeded', 'failed', 'interrupted'}:
                row['remote_state'] = 'uploading'
        dependencies = self._dependency_map()
        for ref, row in rows.items():
            row['url'] = 'https://www.kaggle.com/datasets/' + ref
            row['verification'] = verifications.get(ref)
            row['active_dependencies'] = self._dependencies(ref, dependencies)
            row['deletion'] = next((op for op in reversed(deletions) if op['dataset_ref'] == ref), None)
        visible = [r for r in rows.values() if r.get('remote_state') != 'deleted']
        return {'datasets': sorted(visible, key=lambda r: (r['owner'].casefold(), r['dataset_ref'])),
                'accounts': accounts, 'refreshing': refreshing, 'deletions': deletions,
                'total_bytes': sum(r.get('bytes') or 0 for r in visible)}

    def refresh(self, payload):
        self.service._reject_secrets(payload)
        ids = payload.get('account_ids')
        accounts = self.service.database.list_accounts()
        eligible = {a['id']: a for a in accounts if a['state'] == 'enabled' and a.get('credential_env_ref')}
        if ids is None:
            ids = list(eligible)
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(i not in eligible for i in ids):
            raise ValidationError('Choose enabled authenticated accounts to sync')
        with self._lock:
            if self._stop.is_set():
                raise ConflictError('App is closing')
            if self._refreshing:
                return {'refreshing': True, 'already_running': True}
            self._refreshing = True
        # Two daemon workers keep the API responsive and bound remote concurrency.
        queue = list(dict.fromkeys(ids))
        queue_lock = threading.Lock()
        remaining = [2]
        def worker():
            try:
                while not self._stop.is_set():
                    with queue_lock:
                        if not queue:
                            break
                        aid = queue.pop(0)
                    self._sync_account(eligible[aid])
            finally:
                with self._lock:
                    remaining[0] -= 1
                    if remaining[0] == 0:
                        self._refreshing = False
        for _ in range(2):
            thread = threading.Thread(target=worker, name='kcp-dataset-library-sync', daemon=True)
            self._threads.append(thread)
            thread.start()
        return {'refreshing': True, 'account_count': len(ids)}

    def _sync_account(self, account):
        aid = account['id']
        with self._lock:
            self._data['accounts'][aid] = {'status': 'syncing', 'checked_at': _now()}
            self._save()
        try:
            _, run = self._cli_provider(aid)
            found, token, tokens = {}, None, set()
            for _ in range(40):
                if self._stop.is_set():
                    raise ValidationError('Sync interrupted; previous inventory retained')
                cmd = ['datasets', 'list', '--mine', '--csv', '--page-size', '50']
                if token:
                    cmd += ['--page-token', token]
                rows, token = _csv_page(run(cmd, 90))
                for row in rows:
                    ref = row.get('ref', '')
                    if not _REF.fullmatch(ref) or ref.split('/')[0].casefold() != account['kaggle_username'].casefold():
                        continue  # --mine can also include shared datasets: never claim ownership.
                    raw = row.get('totalBytes', row.get('size', ''))
                    size = int(raw) if str(raw).isdigit() else None
                    found[ref] = {'dataset_ref': ref, 'account_id': aid, 'owner': ref.split('/')[0],
                        'title': row.get('title') or ref, 'bytes': size, 'last_updated': row.get('lastUpdated'),
                        'is_private': None, 'remote_state': 'available', 'synced_at': _now()}
                if not token:
                    break
                if token in tokens:
                    raise ValidationError('Repeated Kaggle page token; inventory not replaced')
                tokens.add(token)
            else:
                raise ValidationError('Dataset pagination limit reached; inventory not replaced')
            with self._lock:
                for ref, old in self._data['rows'].items():
                    if old['account_id'] == aid and ref not in found:
                        old['remote_state'] = 'not_listed'
                for ref, row in found.items():
                    old = self._data['rows'].get(ref, {})
                    row['is_private'] = old.get('is_private')
                    self._data['rows'][ref] = row
                    verification = self._data['verifications'].get(ref)
                    if verification and verification.get('last_updated') and verification['last_updated'] != row['last_updated']:
                        verification['stale'] = True
                    elif verification and not verification.get('last_updated') and row['last_updated']:
                        try:
                            remote_time = datetime.fromisoformat(row['last_updated'].replace('Z', '+00:00'))
                            if remote_time.tzinfo is None:
                                remote_time = remote_time.replace(tzinfo=timezone.utc)
                            verified_time = datetime.fromisoformat(verification['verified_at'])
                            if remote_time > verified_time:
                                verification['stale'] = True
                            else:
                                verification['last_updated'] = row['last_updated']
                        except ValueError:
                            verification['stale'] = True
                self._data['accounts'][aid] = {'status': 'ready', 'checked_at': _now(), 'count': len(found)}
                self._save()
        except Exception as exc:
            with self._lock:
                self._data['accounts'][aid] = {'status': 'error', 'checked_at': _now(),
                    'error': str(exc)[:1200] if isinstance(exc, ValidationError) else 'Sync failed; previous inventory retained'}
                self._save()

    def record_metadata(self, aid, ref, metadata):
        with self._lock:
            row = self._data['rows'].get(ref)
            if row and row['account_id'] == aid:
                row.update(is_private=metadata.get('isPrivate'), privacy_checked_at=_now())
                self._save()

    def register_verification(self, payload, actor):
        self.service._reject_secrets(payload)
        ref, aid = payload.get('dataset_ref'), payload.get('account_id')
        self._owner(aid, ref)
        op = self.service.datasets.get_operation(payload.get('operation_id'))
        if op['dataset_ref'] != ref or op['account_id'] != aid or op.get('kind') != 'upload':
            raise ValidationError('Verification must match the original upload operation')
        files = payload.get('files')
        if not isinstance(files, list) or not files or len(files) > 10000:
            raise ValidationError('Provide a full remote readback SHA-256 file report')
        def normalized(rows):
            result = {}
            for row in rows:
                name, digest, size = row.get('name'), row.get('sha256'), row.get('bytes')
                if not isinstance(name, str) or len(name) > 1000 or name in result or not re.fullmatch('[a-f0-9]{64}', str(digest)) or type(size) is not int or size < 0:
                    raise ValidationError('Invalid checksum report')
                result[name] = (digest, size)
            return result
        reported = normalized(files)
        if reported != normalized(op.get('files', [])):
            raise ValidationError('Readback SHA-256 report differs from uploaded file hashes')
        if payload.get('method') != 'full_remote_readback' or payload.get('private_verified') is not True:
            raise ValidationError('A private full-remote-readback report is required')
        with self._lock:
            row = self._data['rows'].setdefault(ref, {'dataset_ref': ref, 'account_id': aid,
                'owner': ref.split('/')[0], 'title': op.get('title') or ref, 'bytes': op.get('bytes') or sum(size for _, size in reported.values()),
                'last_updated': None, 'is_private': True, 'remote_state': 'available', 'synced_at': None})
            row.update(is_private=True, remote_state='available', privacy_checked_at=_now())
            record = {'method': 'full_remote_readback', 'source': 'imported_report', 'verified_at': _now(),
                      'file_count': len(files), 'bytes': sum(size for _, size in reported.values()),
                      'operation_id': op['id'], 'last_updated': row.get('last_updated'), 'stale': False,
                      'local_evicted': payload.get('local_evicted') is True,
                      'report_sha256': hashlib.sha256(json.dumps(reported, sort_keys=True).encode()).hexdigest()}
            self._data['verifications'][ref] = record
            self._save()
        self.service.database.append_audit(actor, 'dataset.readback_report', 'dataset', ref, record)
        return {'verification': record}

    def delete(self, payload, actor):
        self.service._reject_secrets(payload)
        ref, aid = payload.get('dataset_ref'), payload.get('account_id')
        self._owner(aid, ref)
        if payload.get('confirm_dataset_ref') != ref or payload.get('confirm_delete') is not True:
            raise ValidationError('Type the exact owner/slug and explicitly confirm permanent remote deletion')
        key = payload.get('idempotency_key', '')
        if not isinstance(key, str) or not 8 <= len(key) <= 120:
            raise ValidationError('Deletion requires an idempotency key')
        # Same lock order as upload submit: datasets -> library.
        with self.service.datasets._lock, self._lock:
            old = next((r for r in self._data['deletions'].values() if r['idempotency_key'] == key), None)
            if old:
                if (old['dataset_ref'], old['account_id']) != (ref, aid):
                    raise ConflictError('Idempotency key belongs to a different deletion')
                return {'deletion': dict(old)}
            if self._stop.is_set() or self.blocks_mutation(aid, ref):
                raise ConflictError('App closing or deletion unresolved; check remote state first')
            if any(o['account_id'] == aid and o['status'] not in {'succeeded', 'failed', 'interrupted'} for o in self.service.datasets.list_operations()):
                raise ConflictError('Wait for this account’s upload/settings operation to finish')
            if self._dependencies(ref):
                raise ConflictError('An active or unresolved job may depend on this dataset')
            verification = self._data['verifications'].get(ref, {})
            if verification.get('local_evicted') and payload.get('acknowledge_archive_loss') is not True:
                raise ValidationError('Local copy was removed; explicitly acknowledge loss of the archived copy')
            op = {'id': uuid.uuid4().hex, 'dataset_ref': ref, 'account_id': aid, 'idempotency_key': key,
                  'status': 'queued', 'phase': 'Checking owner and remote dataset', 'created_at': _now(), 'attempts': 0}
            self._data['deletions'][op['id']] = op
            self._save()
        thread = threading.Thread(target=self._delete_remote, args=(op['id'], actor), daemon=True, name='kcp-dataset-delete')
        self._threads.append(thread)
        thread.start()
        return {'deletion': dict(op)}

    def _delete_update(self, op_id, **changes):
        with self._lock:
            self._data['deletions'][op_id].update(changes, updated_at=_now())
            self._save()

    def _delete_remote(self, op_id, actor):
        op = self._data['deletions'][op_id]
        try:
            _, run = self._cli_provider(op['account_id'])
            self._owner(op['account_id'], op['dataset_ref'])
            if self._dependencies(op['dataset_ref']):
                raise ConflictError('An active job started using this dataset')
            directory = self.service.datasets.root / 'delete-checks' / op_id
            # The metadata helper writes only inside KCP-owned temporary storage.
            self.service.datasets._remote_metadata(run, op['dataset_ref'], directory)
            self._delete_update(op_id, status='running', phase='Deletion requested on Kaggle', attempts=1)
            try:
                output = run(['datasets', 'delete', op['dataset_ref'], '--yes'], 180)
                if re.search(r'\b(error|failed)\b', output, re.I):
                    raise ValidationError(output[:1200])
            except Exception:
                # Never replay a mutation after timeout. A read-only check follows.
                self._delete_update(op_id, status='uncertain', phase='Delete response uncertain; checking remote state')
            self._check_deleted(op_id, run)
            self.service.database.append_audit(actor, 'dataset.delete', 'dataset', op['dataset_ref'], dict(op))
        except Exception as exc:
            status = 'uncertain' if op['attempts'] else 'failed'
            self._delete_update(op_id, status=status, phase='Review remote state; delete was not replayed',
                error=str(exc)[:1200] if isinstance(exc, (ValidationError, ConflictError)) else 'Remote deletion could not be verified')

    def _check_deleted(self, op_id, run):
        op = self._data['deletions'][op_id]
        try:
            self.service.datasets._remote_metadata(run, op['dataset_ref'], self.service.datasets.root / 'delete-checks' / op_id / 'after')
        except ValidationError as exc:
            if not re.search(r'\b404\b', str(exc)):
                raise
            # A 403/auth/network failure is NEVER interpreted as deletion.
            self._delete_update(op_id, status='deleted', phase='Confirmed absent on Kaggle (404)')
            with self._lock:
                row = self._data['rows'].setdefault(op['dataset_ref'], {'dataset_ref': op['dataset_ref'],
                    'account_id': op['account_id'], 'owner': op['dataset_ref'].split('/')[0], 'title': op['dataset_ref'],
                    'bytes': None, 'is_private': None, 'last_updated': None, 'synced_at': None})
                row['remote_state'] = 'deleted'
                self._save()
            return
        self._delete_update(op_id, status='uncertain', phase='Dataset is still readable; no repeated delete was sent')

    def reconcile(self, payload):
        self.service._reject_secrets(payload)
        op_id = payload.get('deletion_id')
        with self._lock:
            if op_id not in self._data['deletions']:
                raise NotFoundError('Deletion not found')
            op = dict(self._data['deletions'][op_id])
        self._owner(op['account_id'], op['dataset_ref'])
        _, run = self._cli_provider(op['account_id'])
        try:
            self._check_deleted(op_id, run)
        except ValidationError as exc:
            self._delete_update(op_id, status='uncertain', phase='Remote check failed; no delete replay', error=str(exc)[:1200])
        return {'deletion': dict(self._data['deletions'][op_id])}

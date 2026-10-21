import json
import http.client
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.dataset_library import DatasetLibrary
from control_plane.errors import ConflictError, ValidationError
from control_plane.service import ControlPlaneService
from control_plane.api import create_server
from tests_backend.test_control_plane import account_payload, make_source


class DatasetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = self.root/'experiments'; self.sources.mkdir()
        self.service = ControlPlaneService(self.root/'db.sqlite', data_dir=self.root/'internal', adapter_name='fake',
                                           allowed_source_root=self.sources, start_scheduler=False)
        self.account = self.service.create_account(account_payload('testowner', 'KCP_TEST_LIBRARY'), 'test')
        self.calls = []; self.removed = False; self.after_error = '404 Not Found'
        self.library = DatasetLibrary(self.service, lambda aid: (self.account, self.cli))
        self.service.dataset_library = self.library
        self.service.datasets._cli_provider = lambda aid: (self.account, self.cli)

    def tearDown(self):
        self.service.close(); self.temp.cleanup()

    def cli(self, args, timeout=120):
        self.calls.append(args)
        if args[:2] == ['datasets','list']:
            if '--page-token' not in args:
                return 'Next Page Token = token2\nref,title,size,lastUpdated\ntestowner/sample-data,Sample,1234,2026-10-22\nother/shared-input,Shared,50,2026-10-22\n'
            return 'ref,title,size,lastUpdated\ntestowner/second-data,Second,456,2026-10-22\n'
        if args[:2] == ['datasets','metadata']:
            if self.removed:
                raise ValidationError(self.after_error)
            directory=Path(args[args.index('-p')+1]);directory.mkdir(parents=True,exist_ok=True)
            (directory/'dataset-metadata.json').write_text(json.dumps({'id':args[2],'isPrivate':True,'title':'Sample'}))
            return 'ok'
        if args[:2] == ['datasets','delete']:
            self.removed=True; return 'Dataset deleted successfully'
        return 'ready'

    def payload(self, **updates):
        return dict(account_id=self.account['id'], dataset_ref='testowner/sample-data',
                    confirm_dataset_ref='testowner/sample-data', confirm_delete=True, idempotency_key='library-delete-001', **updates)

    def wait_delete(self, op_id):
        for _ in range(200):
            row=self.library._data['deletions'][op_id]
            if row['status'] not in {'queued','running'}:
                return row
            time.sleep(.01)
        self.fail('Deletion did not finish')

    def test_owned_pagination_filters_shared_and_retains_cache_on_failed_refresh(self):
        self.library._sync_account(self.account)
        data=self.library.snapshot()
        self.assertEqual({r['dataset_ref'] for r in data['datasets']},{'testowner/sample-data','testowner/second-data'})
        self.assertEqual(data['total_bytes'],1690)
        self.assertIsNone(data['datasets'][0]['is_private'])
        with patch.object(self.library,'_cli_provider',side_effect=ValidationError('403 Forbidden')):
            self.library._sync_account(self.account)
        self.assertEqual(len(self.library.snapshot()['datasets']),2)
        self.assertEqual(self.library.snapshot()['accounts'][self.account['id']]['status'],'error')

    def test_delete_requires_exact_owner_and_confirmation_before_any_cli(self):
        for change in [dict(confirm_delete=False),dict(confirm_dataset_ref='wrong/data'),dict(dataset_ref='other/sample-data',confirm_dataset_ref='other/sample-data')]:
            payload=self.payload();payload.update(change)
            with self.assertRaises(ValidationError):self.library.delete(payload,'test')
        self.assertFalse(self.calls)

    def test_delete_uses_official_cli_once_and_confirms_404(self):
        self.library._sync_account(self.account)
        record=self.library.delete(self.payload(),'test')['deletion']
        result=self.wait_delete(record['id'])
        self.assertEqual(result['status'],'deleted',result)
        self.assertEqual(self.library.delete(self.payload(),'test')['deletion']['id'],record['id'])
        self.assertEqual(sum(c[:2]==['datasets','delete'] for c in self.calls),1)
        self.assertIn('--yes',next(c for c in self.calls if c[:2]==['datasets','delete']))
        self.assertNotIn('testowner/sample-data',{r['dataset_ref'] for r in self.library.snapshot()['datasets']})

    def test_403_after_delete_remains_uncertain_and_never_replays(self):
        self.after_error='403 Forbidden'
        record=self.library.delete(self.payload(),'test')['deletion'];result=self.wait_delete(record['id'])
        self.assertEqual(result['status'],'uncertain')
        self.assertTrue(self.library.blocks_mutation(self.account['id'],'testowner/sample-data'))
        self.library.reconcile({'deletion_id':record['id']})
        self.assertEqual(sum(c[:2]==['datasets','delete'] for c in self.calls),1)
        self.assertEqual(self.library._data['deletions'][record['id']]['status'],'uncertain')

    def test_active_job_or_upload_blocks_remote_delete(self):
        with patch.object(self.library,'_dependencies',return_value=[{'job_id':'active'}]):
            with self.assertRaises(ConflictError):self.library.delete(self.payload(),'test')
        with patch.object(self.service.datasets,'list_operations',return_value=[{'account_id':self.account['id'],'status':'running'}]):
            with self.assertRaises(ConflictError):self.library.delete(self.payload(),'test')
        self.assertFalse(self.calls)

    def test_verified_evicted_archive_requires_explicit_loss_acknowledgement(self):
        self.library._data['verifications']['testowner/sample-data']={'local_evicted':True}
        with self.assertRaises(ValidationError):self.library.delete(self.payload(),'test')
        self.assertFalse(self.calls)
        record=self.library.delete(self.payload(acknowledge_archive_loss=True),'test')['deletion']
        self.assertEqual(self.wait_delete(record['id'])['status'],'deleted')

    def test_checksum_report_must_match_original_upload_hashes(self):
        files=[{'name':'payload.bin','bytes':3,'sha256':'a'*64}]
        with patch.object(self.service.datasets,'get_operation',return_value=dict(id='upload1',account_id=self.account['id'],dataset_ref='testowner/sample-data',kind='upload',files=files)):
            payload=dict(account_id=self.account['id'],dataset_ref='testowner/sample-data',operation_id='upload1',files=files,
                         method='full_remote_readback',private_verified=True,local_evicted=True)
            got=self.library.register_verification(payload,'test')['verification']
            self.assertTrue(got['local_evicted']);self.assertEqual(got['file_count'],1)
            with self.assertRaises(ValidationError):self.library.register_verification({**payload,'files':[dict(files[0],sha256='b'*64)]},'test')

    def test_restart_does_not_replay_uncertain_deletion(self):
        self.library._data['deletions']['one']={'status':'running','account_id':self.account['id'],'dataset_ref':'testowner/sample-data'}
        self.library._save()
        restarted=DatasetLibrary(self.service,lambda aid:(self.account,self.cli))
        self.assertEqual(restarted._data['deletions']['one']['status'],'uncertain')
        self.assertFalse(self.calls)

    def test_new_job_cannot_use_dataset_while_deleting(self):
        source=make_source(self.sources,'consumer')
        p=source/'kernel-metadata.json';meta=json.loads(p.read_text());meta['dataset_sources']=['testowner/sample-data'];p.write_text(json.dumps(meta))
        self.library._data['deletions']['one']={'status':'queued','account_id':self.account['id'],'dataset_ref':'testowner/sample-data'}
        with self.assertRaises(ConflictError):self.library.assert_job_inputs_available([{'source_dir':str(source)}])

    def test_remote_update_marks_previous_checksum_report_stale(self):
        self.library._data['verifications']['testowner/sample-data']={
            'last_updated':'2026-10-21','verified_at':'2026-10-21T00:00:00+00:00','stale':False}
        self.library._sync_account(self.account)
        self.assertTrue(self.library._data['verifications']['testowner/sample-data']['stale'])

    def test_http_library_and_bad_delete_confirmation(self):
        self.library._sync_account(self.account)
        server=create_server(self.service,host='127.0.0.1',port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        client=http.client.HTTPConnection(*server.server_address,timeout=3)
        try:
            client.request('GET','/api/datasets/library');response=client.getresponse()
            self.assertEqual(response.status,200);self.assertEqual(len(json.loads(response.read())['datasets']),2)
            payload=self.payload();payload['confirm_dataset_ref']='wrong/other-data'
            client.request('POST','/api/datasets/library/delete',json.dumps(payload),{'Content-Type':'application/json'})
            response=client.getresponse();self.assertEqual(response.status,422);response.read()
            self.assertFalse(any(c[:2]==['datasets','delete'] for c in self.calls))
        finally:
            client.close();server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()

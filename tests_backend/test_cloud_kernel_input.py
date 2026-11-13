import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloud_orchestrator.native_checkpoint import run_kernel_output


class KernelInputTests(unittest.TestCase):
    def make(self,root,name='parent'):
        p=Path(root)/name;p.mkdir()
        for f in ['runtime.json','split_guard.json','validation.json','resume_latest.pth',
                  'metrics_history.json','val_h_best_student.pth','val_h_best_teachers.pth']:
            (p/f).write_bytes(b'fixture')
        (p/'source_manifest.json').write_text(json.dumps(dict(run_sha256='a'*64)))
        return p
    def test_exact_kernel_files_hash_without_dataset_archive_claim(self):
        with tempfile.TemporaryDirectory() as d, patch('cloud_orchestrator.native_checkpoint.validate_directory',return_value={'status':'PASS'}):
            self.make(d);out=Path(d)/'out';out.mkdir()
            r=run_kernel_output(out,dict(source_sha256='a'*64),Path(d))
            self.assertTrue(r['kernel_output_full_SHA_readback'])
            self.assertNotIn('private_archive_full_SHA_readback',r)
            self.assertEqual(len(r['artifact_files']),8)
    def test_missing_file_blocks_validation(self):
        with tempfile.TemporaryDirectory() as d, patch('cloud_orchestrator.native_checkpoint.validate_directory') as v:
            p=self.make(d);(p/'resume_latest.pth').unlink()
            with self.assertRaises(AssertionError):run_kernel_output(d,dict(source_sha256='a'*64),d)
            v.assert_not_called()
    def test_source_drift_or_ambiguity_blocks_validation(self):
        for duplicate in [False,True]:
            with tempfile.TemporaryDirectory() as d, patch('cloud_orchestrator.native_checkpoint.validate_directory') as v:
                self.make(d)
                if duplicate:self.make(d,'duplicate')
                with self.assertRaises(AssertionError):run_kernel_output(d,dict(source_sha256=('a' if duplicate else 'b')*64),d)
                v.assert_not_called()
    def test_files_must_remain_immutable_during_native_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.make(d)
            def mutate(*a):
                (p/'resume_latest.pth').write_bytes(b'changed');return {'status':'PASS'}
            with patch('cloud_orchestrator.native_checkpoint.validate_directory',side_effect=mutate):
                with self.assertRaises(AssertionError):run_kernel_output(d,dict(source_sha256='a'*64),d)


if __name__=='__main__':unittest.main()

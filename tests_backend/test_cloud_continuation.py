import copy
import hashlib
import unittest

from cloud_orchestrator.continuation import stage_child


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.source="CFG = {'name':'parent','start':0,'stop':4800,'arm':'DF65','seed':1337,'max_iterations':15000,'no_test':True,'lr':0.01}\nFILES = {'trainer.py':'frozen scientific bytes'}\nprint(FILES)\n"
        self.native=dict(status='PASS',iteration=4800,seed=1337,job_id='job_'+'a'*32,
            source_sha256=hashlib.sha256(self.source.encode()).hexdigest(),checkpoint_sha256='b'*64,
            full_state_RNG_history=True,source_split_PASS=True,frozen_ViT_PASS=True,best_weights_PASS=True,no_test=True)
        self.kw=dict(name='approved-child',stop=7200,boundaries=[0,4800,7200,9600,12000,14400,15000],payload_filename='resume_latest.pth')
    def stage(self,**kw):return stage_child(self.source,self.native,**dict(self.kw,**kw))
    def test_only_six_transport_fields_can_change(self):
        r=self.stage()
        self.assertEqual(r['config']['lr'],0.01)
        self.assertEqual(r['config']['max_iterations'],15000)
        self.assertTrue(r['FILES_exact'] and r['non_CFG_AST_exact'])
    def test_any_missing_native_check_blocks_child(self):
        for key in ['full_state_RNG_history','source_split_PASS','frozen_ViT_PASS','best_weights_PASS','no_test']:
            old=copy.deepcopy(self.native);self.native.pop(key)
            with self.assertRaises(ValueError):self.stage()
            self.native=old
    def test_source_seed_and_checkpoint_must_match(self):
        for key,value in [('source_sha256','c'*64),('seed',2027),('iteration',6000),('checkpoint_sha256','bad')]:
            old=copy.deepcopy(self.native);self.native[key]=value
            with self.assertRaises(ValueError):self.stage()
            self.native=old
    def test_cannot_skip_declared_boundary(self):
        with self.assertRaises(ValueError):self.stage(stop=9600)
    def test_cannot_accept_arbitrary_checkpoint_path(self):
        for path in ['../resume_latest.pth','C:/secret','bad.bin']:
            with self.assertRaises(ValueError):self.stage(payload_filename=path)


if __name__=='__main__':unittest.main()

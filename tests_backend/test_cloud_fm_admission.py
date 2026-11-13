from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace as S
import unittest
from unittest.mock import patch

from cloud_orchestrator.fm_transport import FMTailTransport


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.p=dict(parent=dict(kernel_slug='datdungdan/parent'),foreign_jobs=[],baseline_inventory={'datdungdan/old':{'v':1}},tail_estimate_hours=[2,3])
        self.proposal=dict(kernel_slug='datdungdan/child',accelerator='gpu',dataset_sources=['owner/runtime'])
        quota=S(gpu_quota=S(total_time_allowed=timedelta(hours=30),time_used=timedelta(hours=29)),quota_refresh_time=None)
        self.api=S(get_accelerator_quota_statistics=lambda _:quota)
        self.ds=S(list_dataset_files=lambda _:S(files=[S()]))
        self.client=S(kernels=S(kernels_api_client=self.api),datasets=S(dataset_api_client=self.ds))
        self.t=FMTailTransport(None)
        @contextmanager
        def account(*a):yield self.client,dict(username='datdungdan')
        self.t.account=account
        self.inv=patch('cloud_orchestrator.fm_transport.inventory',return_value=dict(self.p['baseline_inventory']));self.inv.start();self.addCleanup(self.inv.stop)
        self.probe=patch('cloud_orchestrator.kaggle_monitor.probe',return_value=dict(status='complete'));self.probe.start();self.addCleanup(self.probe.stop)
    def run_admission(self):return self.t.admission(self.p,{},self.proposal)
    def test_positive_small_quota_admits_tail_despite_estimate(self):self.assertTrue(self.run_admission()['ready'])
    def test_zero_gpu_quota_waits(self):
        self.api.get_accelerator_quota_statistics=lambda _:S(gpu_quota=S(total_time_allowed=timedelta(),time_used=timedelta()),quota_refresh_time=None)
        self.assertEqual(self.run_admission()['status'],'WAIT_OFFICIAL_GPU_QUOTA')
    def test_cpu_does_not_need_positive_gpu_quota(self):
        self.api.get_accelerator_quota_statistics=lambda _:S(gpu_quota=S(total_time_allowed=timedelta(),time_used=timedelta()),quota_refresh_time=None)
        self.proposal['accelerator']='cpu';self.assertTrue(self.run_admission()['ready'])
    def test_unknown_remote_notebook_blocks_new_jobs(self):
        with patch('cloud_orchestrator.fm_transport.inventory',return_value={'datdungdan/unknown':{}}):
            self.assertEqual(self.run_admission()['status'],'WAIT_UNKNOWN_OR_CHANGED_ACCOUNT_SESSION')
    def test_existing_unclaimed_child_is_never_overwritten(self):
        with patch('cloud_orchestrator.fm_transport.inventory',return_value={'datdungdan/child':{}}):
            self.assertEqual(self.run_admission()['status'],'STOP_UNCLAIMED_SLUG_EXISTS')
    def test_two_remote_sessions_block_cpu_too(self):
        self.p['foreign_jobs']=[dict(kernel_slug='datdungdan/other')]
        with patch('cloud_orchestrator.kaggle_monitor.probe',return_value=dict(status='running')):
            self.assertEqual(self.run_admission()['status'],'WAIT_ACCOUNT_SLOTS')
    def test_missing_dataset_blocks(self):
        self.ds.list_dataset_files=lambda _:S(files=[])
        with self.assertRaises(ValueError):self.run_admission()


if __name__=='__main__':unittest.main()

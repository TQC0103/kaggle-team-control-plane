"""Read-only admission before any finite DF65 cloud mutation."""
import time

from .transport import KaggleTransport
from .fm_tail import ACCOUNT


def inventory(client,username):
    from kagglesdk.kernels.types.kernels_api_service import ApiListKernelsRequest
    from kagglesdk.kernels.types.kernels_enums import KernelsListViewType,KernelsListSortType
    req=ApiListKernelsRequest();req.user=username;req.group=KernelsListViewType.PROFILE
    req.sort_by=KernelsListSortType.DATE_RUN;req.page_size=20;req.page=1
    response=client.kernels.kernels_api_client.list_kernels(req)
    result={}
    for row in response.kernels:
        if row.ref.split('/')[0].casefold()!=username.casefold():raise ValueError('inventory account mismatch')
        result[row.ref.casefold()]=dict(version=row.current_version_number,kernel_id=row.id,
            last_run=row.last_run_time.isoformat() if row.last_run_time else None)
    return result


class FMTailTransport(KaggleTransport):
    def submit_once(self,proposal):
        from pathlib import Path
        import json
        import hashlib
        root=Path('/tmp/kcp-experiments')
        folder=root/proposal['id']
        if folder.parent!=root or not proposal['id'].startswith('job_'):
            raise ValueError('bounded cloud source directory required')
        folder.mkdir(parents=True,exist_ok=False)
        (folder/'run.py').write_text(proposal['source'],encoding='utf-8',newline='\n')
        metadata=dict(id=proposal['kernel_slug'],title=proposal['kernel_slug'].split('/')[1],code_file='run.py',
            language='python',kernel_type='script',is_private=True,enable_internet=False,
            enable_gpu=proposal['accelerator']=='gpu',enable_tpu=False,
            dataset_sources=proposal['dataset_sources'],kernel_sources=proposal['kernel_sources'])
        (folder/'kernel-metadata.json').write_text(json.dumps(metadata,indent=2))
        if hashlib.sha256((folder/'run.py').read_bytes()).hexdigest()!=proposal['source_sha256']:
            raise ValueError('cloud staged source SHA mismatch')
        return super().submit_once(proposal)

    def admission(self,plan,state,proposal):
        from kagglesdk.kernels.types.kernels_api_service import ApiGetAcceleratorQuotaStatisticsRequest
        from kagglesdk.datasets.types.dataset_api_service import ApiListDatasetFilesRequest
        from .kaggle_monitor import probe
        result=dict(ready=False,status='WAIT_ADMISSION',observed_at=time.time(),account_id=ACCOUNT,
                    accelerator=proposal['accelerator'],wall_hours=plan['tail_estimate_hours'] if proposal['accelerator']=='gpu' else [0.05,0.5])
        with self.account(ACCOUNT) as (client,item):
            current=inventory(client,item['username'])
            known=[plan['parent'],*plan['foreign_jobs'],*[r['job'] for r in state.get('completed',[])]]
            if state.get('training_job'):known.append(state['training_job'])
            allowed={r['kernel_slug'].casefold() for r in known}
            baseline=plan['baseline_inventory']
            if proposal['kernel_slug'].casefold() in current:
                result['status']='STOP_UNCLAIMED_SLUG_EXISTS';return result
            for ref,row in current.items():
                if ref not in allowed and baseline.get(ref)!=row:
                    result['status']='WAIT_UNKNOWN_OR_CHANGED_ACCOUNT_SESSION';return result
            observed=[]
            for row in {r['kernel_slug']:r for r in known}.values():
                observation=probe(client,item,row)
                if observation['status']=='unknown':
                    result['status']='WAIT_UNKNOWN_STATUS';return result
                observed.append(observation)
            active=sum(r['status'] in {'queued','running'} for r in observed)
            result.update(active_jobs=active,reconciled=True)
            if active>=2:
                result['status']='WAIT_ACCOUNT_SLOTS';return result
            q=client.kernels.kernels_api_client.get_accelerator_quota_statistics(ApiGetAcceleratorQuotaStatisticsRequest())
            if q.gpu_quota is None:raise ValueError('official GPU quota unavailable')
            quota=max(0,(q.gpu_quota.total_time_allowed-q.gpu_quota.time_used).total_seconds()/3600)
            result.update(remaining_gpu_hours=quota,refresh_at=q.quota_refresh_time.isoformat() if q.quota_refresh_time else None)
            if proposal['accelerator']=='gpu' and quota<=0:
                result['status']='WAIT_OFFICIAL_GPU_QUOTA';return result
            for ref in proposal['dataset_sources']:
                owner,slug=ref.split('/')
                req=ApiListDatasetFilesRequest();req.owner_slug=owner;req.dataset_slug=slug;req.page_size=1
                response=client.datasets.dataset_api_client.list_dataset_files(req)
                if not response.files:raise ValueError('recipient dataset input unreadable')
            result.update(ready=True,status='ADMISSION_PASS',input_read=True)
            return result

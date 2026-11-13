"""Normal official Kaggle transport for finite, receipted cloud operations.

The caller must persist an intent and account reservation BEFORE submit_once.
This class never retries SaveKernel. Unknown mutations require exact lookup.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from urllib.parse import urlparse, urljoin

from .protocol import SHA, SLUG
from .kaggle_monitor import probe


class TransportError(RuntimeError):
    pass


def read_output_json(url, max_bytes):
    """Official Kaggle output gateway, then a bounded Google Storage redirect.

    No account credentials, cookies or signed URLs leave this function.
    """
    import requests
    for attempt in range(3):
        host = urlparse(url)
        storage = host.hostname == 'storage.googleapis.com' or (host.hostname or '').endswith('.storage.googleapis.com')
        gateway = attempt == 0 and host.hostname == 'www.kaggleusercontent.com'
        if (host.scheme != 'https' or host.username or host.password
                or host.port not in {None, 443} or not (storage or gateway)):
            raise TransportError('UNEXPECTED_OUTPUT_DOWNLOAD_HOST')
        with requests.get(url, stream=True, timeout=(3, 15), allow_redirects=False) as r:
            if r.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, r.headers.get('Location', ''))
                continue
            if r.status_code != 200:
                raise TransportError('OUTPUT_DOWNLOAD_NOT_READY')
            data = bytearray()
            for block in r.iter_content(16384):
                data.extend(block)
                if len(data) > max_bytes:
                    raise TransportError('RECEIPT_OUTPUT_TOO_LARGE')
            return json.loads(data)
    raise TransportError('OUTPUT_REDIRECT_LIMIT')


class KaggleTransport:
    def __init__(self, sessions):
        self.sessions, self._accounts = sessions, None

    @contextmanager
    def account(self, identity):
        if self._accounts is None:
            self._accounts = self.sessions.load()
        with self.sessions.authenticated(self._accounts[identity]) as client:
            yield client, self._accounts[identity]

    def submit_once(self, proposal):
        """No retry decorator; no create on a pre-existing/uncertain intent."""
        from kagglesdk.kernels.types.kernels_api_service import ApiSaveKernelRequest
        if (not SLUG.fullmatch(proposal['kernel_slug'])
                or hashlib.sha256(proposal['source'].encode()).hexdigest() != proposal['source_sha256']
                or proposal['accelerator'] not in {'cpu', 'gpu'}
                or not 1 <= len(proposal['source'].encode()) <= 850000
                or proposal.get('is_private') is not True
                or proposal.get('enable_internet') is not False):
            raise ValueError('invalid immutable cloud proposal')
        if proposal['accelerator'] == 'gpu' and proposal.get('machine_shape') != 'NvidiaTeslaT4':
            raise ValueError('T4 is the only scientific GPU shape')
        with self.account(proposal['account_id']) as (client, item):
            if proposal['kernel_slug'].split('/')[0].casefold() != item['username'].casefold():
                raise ValueError('proposal account identity mismatch')
            req = ApiSaveKernelRequest()
            req.slug, req.new_title = proposal['kernel_slug'], proposal['kernel_slug'].split('/')[1]
            req.text, req.language, req.kernel_type = proposal['source'], 'python', 'script'
            req.is_private, req.enable_internet = True, False
            req.enable_gpu, req.enable_tpu = proposal['accelerator'] == 'gpu', False
            req.machine_shape = 'NvidiaTeslaT4' if req.enable_gpu else None
            req.dataset_data_sources = proposal.get('dataset_sources', [])
            req.kernel_data_sources = proposal.get('kernel_sources', [])
            req.competition_data_sources, req.model_data_sources = [], []
            timeout = proposal.get('timeout_seconds')
            if timeout is not None:
                if type(timeout) is not int or not 1 <= timeout <= 43200:
                    raise ValueError('invalid bounded session timeout')
                req.session_timeout_seconds = timeout
            response = client.kernels.kernels_api_client.save_kernel(req)
            if (response.error or response.invalid_dataset_sources or response.invalid_kernel_sources
                    or response.invalid_competition_sources or response.invalid_model_sources
                    or response.version_number != 1
                    or response.ref.casefold() != proposal['kernel_slug'].casefold()
                    or response.kernel_id <= 0):
                raise TransportError('SAVE_RESPONSE_NEEDS_RECONCILIATION')
            return dict(account_id=proposal['account_id'], kernel_slug=proposal['kernel_slug'],
                        version=1, kernel_id=response.kernel_id, source_sha256=proposal['source_sha256'])

    def observe(self, row):
        with self.account(row['account_id']) as (client, item):
            return probe(client, item, row)

    def reconcile(self, proposal):
        """Adopt only exact source/version1; 403/404 never trigger another POST."""
        row = {k: proposal[k] for k in ['id', 'account_id', 'kernel_slug', 'source_sha256']}
        row['version'] = 1
        return self.observe(row)

    def read_json(self, row, basename, max_bytes=200000):
        from kagglesdk.kernels.types.kernels_api_service import ApiListKernelSessionOutputRequest
        if basename not in {'echo_receipt.json', 'native_receipt.json'} or not 1 <= max_bytes <= 500000:
            raise ValueError('bounded receipt-only output reader')
        with self.account(row['account_id']) as (client, item):
            before = probe(client, item, row)
            if before['status'] != 'complete':
                raise TransportError('PARENT_NOT_COMPLETE')
            owner, slug = row['kernel_slug'].split('/')
            req = ApiListKernelSessionOutputRequest()
            req.user_name, req.kernel_slug, req.page_size = owner, slug, 200
            response = client.kernels.kernels_api_client.list_kernel_session_output(req)
            files = [f for f in response.files if f.file_name.split('/')[-1] == basename]
            if response.next_page_token or len(files) != 1:
                raise TransportError('RECEIPT_OUTPUT_MISSING_OR_AMBIGUOUS')
            result = read_output_json(files[0].url, max_bytes)
            after = probe(client, item, row)
            if after['status'] != 'complete' or after['kernel_id'] != before['kernel_id']:
                raise TransportError('PARENT_IDENTITY_CHANGED')
            return result

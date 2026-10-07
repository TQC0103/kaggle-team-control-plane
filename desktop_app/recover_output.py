"""Read-only completed-output repair through KCP; never submits a kernel.

Run as `python -m desktop_app.recover_output JOB_ID`. Authentication is resolved
internally by the desktop runtime; no credential is accepted or returned.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from control_plane.dataset_transfer import _account_cli
from control_plane.errors import ConflictError, ValidationError
from .main import DesktopRuntime


def recover_output(service, job_id):
    job = service.database.get_job(job_id)
    if job['status'] != 'succeeded':
        raise ConflictError('Only completed successful kernels can recover outputs')
    output = Path(job['output_dir']).resolve()
    if not output.is_relative_to(service.artifact_root.resolve()):
        raise ValidationError('Output directory is outside the managed artifact root')
    if any(p.is_symlink() for p in output.rglob('*')):
        raise ValidationError('Managed output contains a symbolic link')
    output.mkdir(parents=True, exist_ok=True)
    _, run = _account_cli(service, job['account_id'])
    run(['kernels', 'output', job['kernel_slug'], '-p', str(output), '--force'], 600)
    files = sorted(str(p.relative_to(output)) for p in output.rglob('*') if p.is_file())
    if not files or any((output / p).stat().st_size == 0 for p in files):
        raise ConflictError('Recovered output is empty or contains incomplete files')
    result = dict(job.get('result') or {})
    result.pop('output_pending', None)
    result['output'] = {'output_dir': str(output), 'files': files}
    updated = service.database.transition_job(job_id, {'succeeded'}, 'succeeded',
        fields={'result': result, 'finished_at': job.get('finished_at')})
    if not updated:
        raise ConflictError('Job state changed during output repair')
    service.database.append_job_event(job_id, 'Recovered completed output on demand',
                                      details={'file_count': len(files), 'no_kernel_submission': True})
    return {'job_id': job_id, 'status': 'succeeded', 'output_ready': True,
            'file_count': len(files), 'no_kernel_submission': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    args = parser.parse_args()
    # Separate temporary local ports keep the user's installed window open.
    runtime = DesktopRuntime(8766, 3102)
    try:
        runtime.start()
        print(json.dumps(recover_output(runtime.service, args.job_id)))
    finally:
        runtime.stop()


if __name__ == '__main__':
    main()

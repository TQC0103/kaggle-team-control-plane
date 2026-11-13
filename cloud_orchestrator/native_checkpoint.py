"""Portable, read-only native checkpoint validation for the approved FM family.

No trainer, inference, dataset/test access or checkpoint mutation. Torch is
imported only inside validation, in the pinned CPU validation environment.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def finite(x):
    import torch
    if isinstance(x, torch.Tensor):
        assert not x.is_floating_point() or bool(torch.isfinite(x).all()), 'nonfinite tensor'
    elif isinstance(x, dict):
        for v in x.values(): finite(v)
    elif isinstance(x, (tuple, list)):
        for v in x: finite(v)
    elif isinstance(x, float):
        assert math.isfinite(x), 'nonfinite scalar'


def equal(a, b):
    import torch
    if isinstance(a, torch.Tensor):
        return isinstance(b, torch.Tensor) and torch.equal(a, b)
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, (tuple, list)):
        return type(a) is type(b) and len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    return a == b


def tensor_schema(state):
    return {k: dict(shape=list(t.shape), dtype=str(t.dtype)) for k, t in state.items()}


def history_prefix(history):
    return {k: dict(length=len(v), sha256=canonical_sha(v)) for k, v in history.items() if isinstance(v, list)}


def validate_state(state, history, best_student, best_teachers, contract):
    import torch
    cfg = contract['config']
    assert contract['schema'] == 1 and cfg['arm'] in {'DF63', 'DF65'}
    assert state['iter_num'] == cfg['stop'] and state['immutable']['max_iterations'] == 15000
    assert state['immutable']['seed'] == cfg['seed'] == contract['seed']
    assert state['immutable']['FM_ARM'] == cfg['arm']
    for k in ['teacher_math', 'teacher_ling', 'student', 't_optimizer', 's_optimizer',
              't_scheduler', 's_scheduler', 'fm_state', 'history', 'python_rng', 'numpy_rng',
              'torch_rng', 'cuda_rng', 'dose_state', 'best_student_state', 'best_teacher_state']:
        assert k in state, 'missing full checkpoint state'
    finite(state)
    assert state['torch_rng'].dtype == torch.uint8
    assert len(state['cuda_rng']) == contract['expected_cuda_rng_devices']
    assert all(x.dtype == torch.uint8 for x in state['cuda_rng'])
    assert state['dose_state']['generator_seed'] == 20261001 and state['dose_state']['k'] == 0
    assert state['t_scheduler']['last_epoch'] == state['s_scheduler']['last_epoch'] == cfg['stop']
    assert equal(state['history'], history) and not history['validation']
    for key in ['teacher_math', 'teacher_ling', 'student']:
        assert tensor_schema(state[key]) == contract['model_schemas'][key], 'model parameter/buffer schema drift'
    reports = history['val_h']
    assert [r['iteration'] for r in reports] == list(range(200, cfg['stop']+1, 200))
    assert len(history['and_arbitration']) == cfg['stop']//50
    assert len(history['iteration_time']) == cfg['stop']
    for report in reports:
        assert len(report['cases']) == len(cfg['val_h_ids'])
        assert {r['case_id'] for r in report['cases']} == set(cfg['val_h_ids'])
        mean = sum(r['dice'] for r in report['cases'])/len(report['cases'])
        assert abs(mean-report['mean_dice']) < 1e-12
    best = max(reports, key=lambda r: r['mean_dice'])
    assert state['best_dice_s'] == best['mean_dice'] and state['best_iter_s'] == best['iteration']
    assert equal(state['best_student_state'], best_student)
    assert equal(state['best_teacher_state'], best_teachers)
    model = state['student' if cfg['arm'] == 'DF65' else 'teacher_ling']
    frozen = contract['frozen_reference']
    digest = hashlib.sha256()
    for name in frozen['parameter_order']:
        digest.update(name.encode())
        digest.update(model[name].detach().cpu().contiguous().numpy().tobytes())
    assert digest.hexdigest() == frozen['initial_sha256'] == state['fm_state']['initial_frozen_SHA']
    for key, reference in contract['parent_history_prefix'].items():
        assert canonical_sha(history[key][:reference['length']]) == reference['sha256'], 'history prefix drift'
    return dict(endpoint_Dice=reports[-1]['mean_dice'], best_Dice=best['mean_dice'],
                best_iteration=best['iteration'], history_prefix=history_prefix(history))


def validate_directory(directory, contract):
    import torch
    torch.set_num_threads(2)
    directory = Path(directory)
    def read(name): return json.loads((directory/name).read_text(encoding='utf-8'))
    cfg = contract['config']
    manifest, runtime, guard = read('source_manifest.json'), read('runtime.json'), read('split_guard.json')
    assert manifest['run_sha256'] == runtime['source_sha256'] == contract['source_sha256']
    assert manifest['files'] == contract['FILES_SHA256'] and manifest['config'] == cfg
    assert runtime['torch'] == '2.10.0+cu128' and '3.12.13' in runtime['python']
    assert runtime['devices'] and all(d['capability'] == [7, 5] for d in runtime['devices'])
    assert guard['intersection'] == guard['test_linked'] == 0
    assert guard['verified_train_val_h5'] == len(cfg['train_ids']+cfg['val_h_ids'])
    assert guard['train'] == cfg['train_ids'] and guard['VAL_H'] == cfg['val_h_ids']
    assert guard['labeled'] == cfg['train_ids'][:cfg['labeled_num']]
    assert set(cfg['train_ids']).isdisjoint(cfg['val_h_ids'])
    cp = directory/'resume_latest.pth'
    checksum = sha(cp)
    assert read('validation.json')['checkpoint_sha256'] == checksum
    if contract.get('known_checkpoint_sha256'):
        assert checksum == contract['known_checkpoint_sha256']
    state = torch.load(cp, map_location='cpu', weights_only=False)
    values = validate_state(state, read('metrics_history.json'),
        torch.load(directory/'val_h_best_student.pth', map_location='cpu', weights_only=False),
        torch.load(directory/'val_h_best_teachers.pth', map_location='cpu', weights_only=False), contract)
    return dict(status='PASS', contract_sha256=canonical_sha(contract),
                job_id=contract['job_id'], source_sha256=contract['source_sha256'],
                checkpoint_sha256=checksum, seed=cfg['seed'], iteration=cfg['stop'],
                full_state_RNG_history=True, source_split_PASS=True, frozen_ViT_PASS=True,
                best_weights_PASS=True, no_test=True, validation_backend='portable-native-cpu', **values)


def run_archive(root, contract, mappings, input_root):
    """Kaggle CPU entry: read only exact declared artifact files, never images."""
    root, input_root = Path(root), Path(input_root)
    allowed = {'source_manifest.json', 'runtime.json', 'split_guard.json', 'validation.json',
               'resume_latest.pth', 'metrics_history.json', 'val_h_best_student.pth', 'val_h_best_teachers.pth'}
    assert set(mappings) == allowed
    native = root/'native-input'
    native.mkdir()
    try:
        for name, info in mappings.items():
            assert Path(info['name']).name == info['name']
            candidates = [p for p in input_root.rglob(info['name']) if p.is_file()]
            assert len(candidates) == 1, 'missing/ambiguous private artifact payload'
            source = candidates[0]
            assert source.stat().st_size == info['bytes'] and sha(source) == info['sha256']
            (native/name).symlink_to(source)
        proof = validate_directory(native, contract)
        proof['private_archive_full_SHA_readback'] = True
        (root/'native_receipt.json').write_text(json.dumps(proof, sort_keys=True, indent=2)+'\n', encoding='utf-8')
        print('NATIVE_CHECKPOINT_VALIDATION_PASS', proof['iteration'], proof['checkpoint_sha256'], flush=True)
    finally:
        # Do not cause Kaggle output collection to duplicate mounted model files.
        assert not native.is_symlink() and native.resolve().parent == root.resolve()
        for name in allowed:
            p = native/name
            if p.is_symlink(): p.unlink()
        native.rmdir()


def run_kernel_output(root, contract, input_root):
    """Native CPU worker over an exact same-owner private kernel output.

    The coordinator must source-fence that completed kernel separately. This
    helper finds its source manifest and hashes only its eight required files;
    it does not claim that an independent dataset archive was created.
    """
    input_root=Path(input_root)
    candidates=[]
    for path in input_root.rglob('source_manifest.json'):
        if not path.is_file() or path.stat().st_size>2000000:
            continue
        manifest=json.loads(path.read_text())
        if manifest.get('run_sha256')==contract['source_sha256']:
            candidates.append(path.parent)
    assert len(candidates)==1, 'missing or ambiguous exact source output'
    folder=candidates[0]
    allowed=['source_manifest.json','runtime.json','split_guard.json','validation.json',
             'resume_latest.pth','metrics_history.json','val_h_best_student.pth','val_h_best_teachers.pth']
    hashes={}
    for name in allowed:
        path=folder/name
        assert path.is_file() and path.stat().st_size>0, 'incomplete kernel output'
        hashes[name]=dict(bytes=path.stat().st_size,sha256=sha(path))
    result=validate_directory(folder,contract)
    assert all(sha(folder/name)==item['sha256'] for name,item in hashes.items()), 'kernel output changed during validation'
    result.update(kernel_output_full_SHA_readback=True,artifact_files=hashes)
    (Path(root)/'native_receipt.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return result

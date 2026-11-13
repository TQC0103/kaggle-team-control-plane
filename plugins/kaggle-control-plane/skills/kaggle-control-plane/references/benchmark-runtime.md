# Benchmark runtime and log diagnosis

Read this reference only when preparing, optimizing, or interpreting a Kaggle
model benchmark.

## Reproducible job source

- Keep one narrow source bundle with `kernel-metadata.json` and one explicit
  entry point. Record source hashes and the public dataset URL/hash.
- Pin packages that determine CUDA/model behavior. Emit a bounded `runtime.json`
  with Python, Torch, CUDA, device, model-library versions, and a
  resolved-requirements hash.
- Verify `torch.cuda.is_available()`, device name, compute capability `(7, 5)`,
  and one real CUDA operation before the expensive phase.
- Add `wrapt` when Kaggle's `sitecustomize` emits `ModuleNotFoundError: wrapt`.
  It is non-fatal if metrics continue, but should not remain in the next bundle.
- Do not commit predictions, datasets, model caches, environments, logs, or
  result archives. Preserve final source scripts and metric summaries only.

## Isolated Kaggle dependencies

- Always create a job-specific virtual environment inside `/kaggle/working`,
  normally with `python -m venv --system-site-packages <job>-venv`. Install
  only required extra packages using that venv's Python and execute training
  and evaluation with that same interpreter.
- Never run `pip install` against Kaggle's system interpreter or replace its
  preinstalled CUDA, PyTorch, torchvision, or driver-related packages. The
  `--system-site-packages` flag makes the compatible Kaggle runtime visible
  while keeping additions isolated.
- Record the venv package versions and remove the venv before final artifact
  collection unless the user explicitly requests an environment artifact.

## Multi-GPU use

- During CUDA preflight, record `torch.cuda.device_count()`, every visible
  device name, and compute capability. Do not assume that a Kaggle T4 request
  exposes two GPUs; use only the devices actually reported at runtime.
- When two compatible T4 GPUs are visible and the workload can safely support
  it, use both (for example, `DataParallel` for an existing single-process
  PyTorch training loop) and log the device IDs plus per-device/global batch
  size. Do not leave a hard-coded `CUDA_VISIBLE_DEVICES=0` in a future bundle.
- Keep the one-GPU path working and select it automatically when only one GPU
  is visible. For strict reproductions, record the multi-GPU change separately
  because its batching and numerical trajectory can differ from the upstream
  single-GPU run.

## Resumable training and metrics

- For any non-trivial training run, write an atomic `resume_latest` checkpoint
  at a bounded interval and at the final epoch. It must contain epoch, model,
  optimizer, scheduler, AMP scaler, EMA/teacher state when applicable, full
  metric history, immutable run configuration, and Python/NumPy/Torch/CUDA RNG
  states. Preserve the best-validation checkpoint separately.
- Write train and validation metrics every epoch to a durable CSV or JSON, and
  include checkpoint hashes plus the exact resume/evaluation commands in a
  manifest. A resume path must validate the saved configuration before loading
  and continue at the following epoch.
- Do not claim that a file in `/kaggle/working` alone survives a failed or
  timed-out batch run. For timeout-safe continuation, split work into bounded
  chunks that finish within Kaggle's limit, publish each completed chunk's
  checkpoint as a durable notebook output/dataset/model, attach it to the next
  chunk, and smoke-test loading it before the next expensive phase. If no
  durable handoff route is available, state that timeout-safe resume cannot be
  guaranteed.

### Chained chunk submission

For a multi-chunk training run, make the handoff automatic and restart-safe.
Keep a small, atomically written local chain-state JSON outside the submitted
source bundle. It must record the current job ID, its Kaggle kernel slug, the
next target iteration/epoch, immutable run configuration, and the original
data kernel or dataset reference. On restart, read that state and query the
Control Plane job before taking any action; never infer progress from a PID or
stale local log.

Only submit the next chunk after the preceding Control Plane job is
`succeeded`, which means its output was downloaded. The next source bundle must
attach both the immutable data source and the immediately previous kernel
output, then locate and validate `resume_latest` before training. Treat a
failed, cancelled, or remotely uncertain job as a stop condition and report it
instead of retrying or skipping ahead automatically.

Use chunk boundaries that are safe for the upstream data loader. When exact
resumption requires an epoch boundary, choose a boundary divisible by the
number of batches per epoch and record that fact. A chain runner may be a
local helper when the Control Plane has no native chain feature, but it must
persist state, avoid concurrent successor submissions, and be explicitly
recoverable after desktop restart.

## Efficient generation

- For decoder-only models set `tokenizer.padding_side = "left"` before
  batching. Repeated right-padding warnings mean the batch is semantically
  unsafe, not merely noisy.
- Pass a dataset/iterable to Transformers pipelines instead of invoking the
  pipeline once per example. Record logical model calls and pipeline batch
  calls separately.
- With deterministic generation (`do_sample=False`), omit `temperature`,
  `top_p`, and sampling `top_k`; Transformers otherwise ignores them.
- Bound batch size and output length for a T4. Run a small smoke subset before
  the full suite. Do not retry a completed configuration just to clean a
  non-fatal warning.

## Log interpretation

- Successful install, CUDA preflight, metric files, zero missing predictions,
  aggregate summary, and final notebook conversion mean the run completed even
  if non-fatal warnings appeared earlier.
- `Recall@K` is annotated-evidence coverage, not answer accuracy. Always report
  the eligible denominator. Evidence F1 and answer F1 are separate metrics.
- Retrieval comparisons are clean only when dataset, chunking, K, and scoring
  are identical. Answer comparisons are not apples-to-apples if only one arm
  invokes a generator.
- In R11, 1,005 `model_calls` and one `generation_batch_calls` meant one batched
  pipeline pass over 1,005 logical cases, not one model inference.

## Live remote logs

Kaggle CLI live output requires:

```text
kaggle kernels logs --follow <owner>/<slug>
```

Without `--follow`, the command does not provide the intended live stream.
Capture a bounded snapshot (up to eight seconds, to allow the Kaggle CLI to
attach to its SSE stream), poll around every 30 seconds, store only new lines,
redact credentials, and treat a temporary fetch failure as a warning rather
than a job failure. The snapshot reader must never block the scheduler when a
Windows child process retains an inherited stdout pipe.

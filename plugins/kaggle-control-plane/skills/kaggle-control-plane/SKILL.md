---
name: kaggle-control-plane
description: Use the local Kaggle Control Plane to inspect accounts and quota, run or monitor Kaggle GPU and batch workloads, download results, diagnose benchmark logs, or maintain the Control Plane desktop app without exposing credentials.
---

# Kaggle Control Plane

Keep Kaggle credentials inside the installed Control Plane. Never read its
credential files, call credential-inspection endpoints, copy DPAPI blobs, or
place secrets in a repository, prompt, log, job source directory, or tool
result.

## Route only to the needed guidance

- For account, quota, submission, monitoring, cancellation, retry, or artifact
  operations, follow **Remote workflow** below.
- When packaging or diagnosing a model benchmark, read
  [references/benchmark-runtime.md](references/benchmark-runtime.md).
- When changing, rebuilding, packaging, or troubleshooting the Control Plane
  desktop app/plugin, read
  [references/desktop-maintenance.md](references/desktop-maintenance.md).

Do not load both references unless the task genuinely spans both modes.

## Remote workflow

1. Call `kcp_status`. If offline, call `kcp_open_app`, allow startup, and retry
   once. Do not silently fall back to a heavy laptop run.
2. Before submission, list accounts and use only an enabled account with
   relevant remaining quota. Prefer a user-named account; otherwise select the
   best available account and report the choice.
3. Stage the narrow source bundle inside the Control Plane's allowed
   `experiments` source root before submission. It must contain
   `kernel-metadata.json`; never submit a repository root, home directory,
   credential directory, or an unrelated workspace directly. If the bundle was
   prepared elsewhere, copy it into a new explicit subdirectory of the allowed
   `experiments` root, validate the staged copy, and submit that staged path.
   Do not overwrite an existing experiment bundle; use a versioned directory.
4. For GPU jobs use exact shape `NvidiaTeslaT4`. Do not use the generic/default
   P100 path: the current PyTorch image lacks Pascal `sm_60` kernels. For TPU
   use `TpuV38`; omit shape for CPU.
5. Require clear authorization immediately before submit, cancel, or retry.
   Prior authorization in the active request is sufficient. Keep runs bounded:
   at most ten jobs per batch and two concurrent jobs per account. Supply an
   `idempotency_key` and reuse it only when retrying the exact same batch after
   an uncertain response; a changed request requires a new key.
6. Poll with `kcp_list_jobs` or `kcp_get_job` at bounded intervals. Treat
   normalized `accelerator`, `machine_shape`, `elapsed_seconds`, and optional
   `runtime` as authoritative.
7. Download only to an explicit project output directory. Runtime results stay
   ignored by Git.
8. Report account ID, shape, source directory, kernel slug, job ID, final state,
   and artifact location. Omit usernames unless requested.

When troubleshooting needs a handoff, prefer `kcp_download_support_bundle` to
manual log/database collection. It contains allow-listed aggregate diagnostics
and build identity, never credentials, usernames, paths, job logs, or artifacts.

Use the laptop for unit tests, formatting, schema validation, dataset parsing,
and short smoke checks only. Use Control Plane for embeddings, batch model
evaluation, training, and other GPU-heavy work.

## Persistent chained benchmarks

For a long benchmark that must continue across Kaggle runtimes, use a small
local chain runner outside submitted source bundles. Its state must be written
atomically under the Control Plane runtime directory, contain the predecessor
job ID/kernel/target and immutable data source, and be restart-safe. A runner
may submit only after the predecessor is `succeeded` and the downloaded
artifact contains a nonempty `resume_latest` checkpoint plus metric history.
For each successor, create a new versioned experiment bundle, attach the
previous kernel output and original data source, retain an isolated venv, and
carry checkpoint frequency/full RNG state forward. Stop on `failed` or
`cancelled`; never retry or skip a chunk automatically.

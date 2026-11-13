---
name: kaggle-control-plane
description: Use the local Kaggle Control Plane to inspect accounts and quota, run or monitor Kaggle workloads, proactively archive validated research artifacts to private Kaggle datasets, diagnose logs, or maintain the desktop app without exposing credentials.
---

# Kaggle Control Plane

Keep Kaggle credentials inside the installed Control Plane. Never read its
credential files, call credential-inspection endpoints, copy DPAPI blobs, or
place secrets in a repository, prompt, log, job source directory, or tool
result.

## Route only to the needed guidance

- For account, quota, submission, monitoring, cancellation, retry, or artifact
  operations, follow **Remote workflow** below.
- After collecting/validating research outputs, or when continuing a project
  with an archive queue, follow **Proactive private artifact archiving** and
  [references/artifact-cloud.md](references/artifact-cloud.md).
- When packaging or diagnosing a model benchmark, read
  [references/benchmark-runtime.md](references/benchmark-runtime.md).
- When changing, rebuilding, packaging, or troubleshooting the Control Plane
  desktop app/plugin, read
  [references/desktop-maintenance.md](references/desktop-maintenance.md).
- For competition accelerators or Internet-disabled notebooks, read
  [references/competition-offline.md](references/competition-offline.md).

Do not load both references unless the task genuinely spans both modes.

## Remote workflow

1. Call `kcp_status`. If offline, call `kcp_open_app`, allow startup, and retry
   once. Do not silently fall back to a heavy laptop run.
2. Before submission, list accounts and use only an enabled account with
   relevant remaining quota. Prefer a user-named account; otherwise select the
   account using the packing preference below and report the choice.
3. Stage the narrow source bundle inside the Control Plane's allowed
   `experiments` source root before submission. It must contain
   `kernel-metadata.json`; never submit a repository root, home directory,
   credential directory, or an unrelated workspace directly. If the bundle was
   prepared elsewhere, copy it into a new explicit subdirectory of the allowed
   `experiments` root, validate the staged copy, and submit that staged path.
   Do not overwrite an existing experiment bundle; use a versioned directory.
4. Default GPU shape is `NvidiaTeslaT4`. Advanced competition shapes require
   the competition/offline workflow linked above. Do not use the generic/default
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
   ignored by Git. After validation, enqueue retained bulky artifacts for private
   cloud archiving as part of the same workflow; do not wait for a cleanup request.
8. Report account ID, shape, source directory, kernel slug, job ID, final state,
   and artifact location. Omit usernames unless requested.

### Account packing preference

The user prefers to use small remaining GPU balances first. Apply this to
already-authorized jobs; it does not create new experiments or change scientific
gates.

- Get fresh official quota, account state, active sessions and input permissions.
  Respect a user-named account first. Otherwise prefer eligible accounts with a
  small positive balance around one hour (use 0 < remaining <= 2 GPU-hours as a
  practical band), ahead of accounts with large unused balances.
- Within that band, prefer an account with two free GPU slots and use the smaller
  positive balance first. Assign the longest estimated ready jobs first. If only
  one slot is free, use it and place the next job on another eligible account;
  do not idle ready work waiting for a perfect pair.
- Estimate each job's wall time and GPU-hours, including setup, training,
  validation and output saving. Print the range, account ID, remaining quota
  and proposed pair before submission. Do not reject an otherwise eligible
  account solely because either estimate or the pair's total exceeds its
  remaining quota; let Kaggle decide admission.
- Pack two independent, input-validated jobs into one bounded idempotent batch
  for that account when both slots are free. Keep the two-jobs/account limit,
  reconcile unknown sessions first, and verify each exact remote notebook/version
  before calling it submitted. Never pack a child before its parent validates.
- A small positive balance is not a guarantee that Kaggle will admit two jobs or
  let them finish after the balance reaches zero. Preserve full-state checkpoints
  for continuation. On an actual quota refusal, record it and inspect the next
  eligible account's quota and input access; do not blindly resubmit or alter
  quota records.

### Cancellation identity and confirmation

Normal Cancel automatically resolves the actual numeric `kernel_session_id`
through the official output-download redirect for the job's exact owner/slug
and version from its original push receipt. It validates the response's exact
HTTPS host and session-specific download_zip path without downloading output.
Status/GetKernel alone do not provide the ID. Explicit `kernel_session_id` plus
matching `session_ref` remains available. Never substitute notebook ID, version
number, or guessed ID, delete the notebook, or push a replacement to stop it.
If the saved version changed, receipt/redirect is missing, or resolution fails,
keep the job/account reserved and report the failure. Do not ask the user for
an ID before attempting KCP's automatic resolver. A KCP job not yet submitted
can be cancelled locally.
An accepted remote request is only `cancel_requested`; only Kaggle terminal-state
verification permits `cancelled`. Do not replay an ambiguous cancellation after a
timeout. The desktop Cancel action and `kcp_job_action` accept the explicit ID.

When troubleshooting needs a handoff, prefer `kcp_download_support_bundle` to
manual log/database collection. It contains allow-listed aggregate diagnostics
and build identity, never credentials, usernames, paths, job logs, or artifacts.

Use the laptop for unit tests, formatting, schema validation, dataset parsing,
and short smoke checks only. Use Control Plane for embeddings, batch model
evaluation, training, and other GPU-heavy work.

## Proactive private artifact archiving

KCP has a persistent automatic archive queue. Use `kcp_artifact_archive` to
inspect it and `kcp_configure_artifact_archive` to enable it under the user's
standing authorization. Enabled applies to every future completed run, not a
fixed experiment list; include current job IDs when enabling for an active
project. The app resumes discovery, transfer reconciliation and readback on
startup without an agent prompt. See `references/artifact-cloud.md` for limits.

For this user's approved research artifacts, private cloud archiving is a
standing preference and part of completing a run. Act on it without asking the
user to repeat an upload request. This covers existing authorized project
outputs and newly validated checkpoints, probability caches and retained data;
it does not authorize public visibility, new readers, remote deletion or GPU jobs.

- On result validation and project continuation, reconcile the durable archive
  queue, then start or resume ready transfers if no live owner already handles
  them. Use bounded parallel uploads on separate eligible accounts; GPU-hour
  exhaustion alone does not prevent dataset storage.
- Upload immutable, deduplicated payloads to private datasets, verify full remote
  byte sizes and SHA-256, persist restore metadata and register the proof in KCP.
- Where the user has authorized local cleanup, evict only verified copies that
  no active job/controller/validator needs. Keep small reports and restore indexes
  local. An upload receipt or inventory listing alone never permits eviction.
- Before handing off, record what actually completed, what is transferring, and
  any blocker. A stale RUNNING label is not a live uploader. A skill is agent
  guidance, not a daemon: only claim background transfer when a live worker with
  a durable journal exists. See the reference for recovery and protection rules.

## Dataset inventory and archived results

Use `kcp_dataset_library` to list the cached owned-dataset inventory across accounts, reported remote bytes, upload status, imported full-readback SHA-256 reports, local-copy eviction and deletion state. Use `kcp_sync_dataset_library` for an asynchronous remote refresh (optionally selected account IDs). It lists only datasets owned by the selected account; shared inputs must not be presented as owned. Account sync errors retain the previous cache and its error/time. Refreshing a table never uploads or deletes data.

An upload/visibility success is not a checksum proof. `/api/datasets/library/verification` registers a full remote-readback report only when every name/byte size/SHA matches the recorded upload operation. Imported reports are labeled as reports; later observed dataset updates make the report stale. Archive runners should register before local eviction and update `local_evicted` after verified local cleanup. Keep the external restore index and receipts.

Only call `kcp_delete_dataset` when the human explicitly authorizes deletion of that exact `owner/slug`; implementing the feature or listing datasets is not authorization to delete real data. Supply the owning account, exact `confirm_dataset_ref`, `confirm_delete=true` and an idempotency key. When a verified archive's local copy was removed, require explicit acknowledgement of losing that archived copy. Active job inputs and active upload/settings operations block deletion. Never delete as an inference or cancellation workaround.

An accepted delete is pending. Only the exact owner lookup returning remote 404 confirms absence; 403, auth failures, timeouts and continued readability remain uncertain. After an uncertain response or app restart, use `kcp_check_dataset_deletion`; never replay the mutation automatically. Deleting remote data does not clean unrelated local files.

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

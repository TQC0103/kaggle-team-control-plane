# Proactive private research artifact storage

## Built-in persistent queue

KCP's `GET /api/artifacts/archive` shows the automatic queue; `POST
/api/artifacts/archive/config` accepts `enabled`, optional `account_ids`, and
`include_job_ids` for existing runs. Enabling once is authorization for private
backup of every subsequent completed run, including successor chunks. The app
persists configuration/manifests/operation IDs/per-file readback/restore entries
in its runtime artifact-archive/queue.json and restarts its worker on app startup.
The Datasets tab exposes this setting and per-run progress. Local originals are
retained by this engine; only disposable verified staging links are cleaned.

Output download must be complete before enqueueing. The engine checks output
paths and SHA and archives retained model/cache/report formats, excluding copied
input/runtime directories (data, msd, venv, wheels). Its VERIFIED status certifies
private remote bytes, not scientific validity of metrics: the project's native
scientific validator is still required before reporting results or continuing
a scientific chain. Global enable is not permission for new GPU work.

Recovery preserves one operation identity. A pre-create interruption resumes
the persisted source plan. A possibly accepted create is reconciled remotely
and read back instead of uploading twice. Only two exact metadata404 reads at
least120s apart may reopen the same missing upload;403/timeouts retain originals
and retry read-only reconciliation. Previously verified files are reused only
with the same remote version marker; without one they are checked again. This
is recovery by phase/file, not a promise that Kaggle supports byte-offset upload
resume. The app must be running and online; while closed it retains the queue.

Legacy uploads without a durable recovery plan remain held for reconciliation;
do not claim that enabling this queue repaired historical403 records. Agent
guidance below complements the built-in queue for external/older artifact work.

Read after collecting or validating research outputs, while continuing an
existing archive queue, or when local storage pressure is reported. Archiving
is a normal post-validation step for this user's approved research projects;
do not wait for the user to ask again. Honor an explicit project hold or a
later instruction to keep a particular file local.

## Trigger and scope

- After a result is downloaded and validated, enqueue the retained bulky files:
  full-state checkpoints, endpoint/best weights, inference probability/mask
  caches, H5 data and other reproducibility payloads. This includes approved
  SLURM outputs once collected; do not alter the cluster's retention rules.
- At project continuation and before handoff, inspect unfinished archive work
  and newly validated files not covered by its frozen manifest. A previous
  COMPLETE status covers that manifest only, not artifacts created later.
- Keep code, reports, metrics, split locks, manifests, receipts, restore indexes
  and small validation evidence locally. Do not sweep a home directory, virtual
  environment, credential store or unrelated project into an archive.
- Existing project authorization covers private uploads of these artifacts.
  Automatic local removal additionally requires the user's cleanup authority
  for the relevant scope; this project has that authority only after full remote
  verification. In another project without it, archive and retain the local copy.
  Sharing, public visibility and deleting remote datasets require their own
  explicit authorization. Storage work never opens an experimental arm or GPU job.

## Durable queue and ownership

1. Read the project's owner registry, queue, intents, receipts, restore index
   and live process/heartbeat. One coordinator owns a queue; never start a
   duplicate while its worker is alive. Check actual operation phases as well
   as cached labels. Recover a dead worker only after reconciling remote state.
2. Freeze each candidate's path, byte size, SHA-256 and run provenance. Deduplicate
   content and aliases against verified objects before uploading. Recheck that
   source bytes did not change before staging. Exclude files still being written
   or needed by an active trainer, validator, continuation or inference worker.
3. Use an existing project archive helper only after checking its current inputs,
   protected paths and recovery behavior. For example, the DualFete project has
   `scripts/parallel_private_archive_20261106.py` with a frozen Nov06 plan and
   `outputs/storage-archive-20261022/restore_index.json`; that old plan must not be
   presented as covering new files. New candidates require a new manifest/version.
4. Save explicit states such as PENDING, UPLOADING, VERIFYING, VERIFIED,
   LOCAL_EVICTED and BLOCKED, plus exact account/dataset/operation IDs and
   idempotency keys. Persist intent before mutation and receipt after acceptance.
   Resolve uncertain operations by read-only lookup; do not blindly replay them.

## Transfer without filling the disk again

- Discover the installed KCP upload/operation capabilities. Use those tools or
  the authenticated local KCP API/project helper; keep authentication inside KCP.
  Dataset-library refresh is read-only inventory, not an upload or checksum test.
- Pick enabled accounts with storage capacity and permission for private datasets.
  GPU-hour balance is a compute admission concern, not the criterion for storing
  artifacts. Use separate accounts for concurrent transfers, one active archive
  upload/readback lane per account unless KCP provides a stricter limit. Start
  conservatively and keep the project's bounded worker/disk budget.
- Prefer exact existing files or hardlink staging on the same volume. Avoid a
  second full archive copy or downloading entire kernel outputs merely to archive
  files already available locally. Keep room for readback, active training output
  and temporary files; pause new staging when the established disk reserve fails.
- Upload only the frozen, narrow payload set with private visibility and a
  manifest. Preserve scientific source hashes, original filenames and parent
  checkpoint provenance even if stored blobs use content-addressed names.
- If a held account or ambiguous operation blocks one lane, keep its originals
  and reconcile that lane. Continue independent, unambiguous ready work on other
  eligible lanes when the owner supports it; do not stall the entire backlog
  solely because one lane is held. Never move an uncertain upload to a second
  account as a substitute for reconciliation.

## Verification, restoration and local removal

1. Confirm the exact dataset/version and private visibility. Read every uploaded
   object's remote bytes back through the approved verifier and compare filename,
   byte size and SHA-256 to the manifest. Streaming or bounded readback can avoid
   retaining a second full local copy. Upload success, dataset presence and total
   size alone are insufficient. An unverified/failed artifact can be retained as
   evidence but must not be labeled scientifically validated.
2. Persist a restore entry mapping original path/run to account, dataset/version,
   blob name, bytes and digest, with upload receipt and verification proof. Keep
   this index and evidence local. Register full-readback proof through KCP's
   `/api/datasets/library/verification` capability before local eviction.
3. Immediately before removal, recheck the same local bytes/SHA, active dependency
   registry and all hardlink aliases. A copied or linked resume input can still
   be needed by a pending continuation. Leave needed copies in place even after
   archiving; mark local eviction pending and revisit when the dependency ends.
4. Under the existing cleanup authorization, remove only explicitly verified,
   inactive payload paths within the approved workspace/output roots. Update
   the durable queue, restore index and KCP `local_evicted` state afterwards.
   Measure physical drive free space separately from logical archived bytes;
   hardlinks and newly generated outputs make those quantities differ.
5. Restore only the files needed for the next task; check SHA again after fetch.
   A stale verification after a remote version change cannot authorize removal.

## Completion and recovery

Do not finish a result-handling turn with an unexplained unprocessed archive
queue. Either finish a bounded transfer, leave one verified live coordinator
working on ready objects, or record the exact blocker and retained bytes/paths.
Report confirmed archived/verified/evicted counts separately from in-flight work.

On 403, timeout, KCP restart or a lost response, preserve local originals and
reconcile the exact operation/dataset/version without replaying mutations. One
bounded read-only retry is appropriate for a transient error; recurring failure
stays BLOCKED with evidence. Do not infer absence from an owned-dataset listing
or 403. Never call a dead process RUNNING because its state file says so.

This skill does not run while the agent is idle or the computer is off. A bounded
restart-safe worker may continue after a turn only when actually launched and
recorded. Do not create a recurring automation merely to implement this policy.

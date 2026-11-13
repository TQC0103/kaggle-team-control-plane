# KCP finite cloud coordinator — shadow release

## Additional implemented stages

The original shadow stage below has since gained private-vault enrollment,
exact-version Kaggle monitoring, a bounded CPU fixture coordinator, and a finite
DF65 continuation controller with native checkpoint validation. These modules
are separately gated; deploying the shadow service does not activate them.
The DF65 plan is deliberately pinned to one approved research continuation,
not a general-purpose scheduler. Its training writer can be fenced and handed
to SLURM; do not reactivate it without reconciling ownership and receipts.

The desktop artifact archiver is separate. There is no cloud artifact-upload
service in this source tree. A cloud scheduler alone does not eliminate the
PC dependency of desktop transfers. Do not include local credentials, state,
experiment bundles or model weights in an image or Git commit.

Cloud native-checkpoint tests require a CPU PyTorch installation in addition
to the cloud service requirements. The service container itself does not run
the model; native validation uses a separately isolated approved CPU workload.

## Original shadow stage

This is the **first implementation stage**, not a working unattended training
chain. It has no submit/cancel/archive adapter and cannot acquire local training
ownership. A deployed instance can record decisions from sealed chain documents;
it cannot submit a child. An unrefreshed imported observation expires after 120 s.

The current desktop and research controllers remain unchanged and authoritative.
Do not tell users their PC can be switched off for chunk continuation yet.

## CPU check

From the repository root:

```text
python -m unittest tests_backend.test_cloud_orchestrator -v
python -m cloud_orchestrator.preflight --project YOUR_PROJECT_ID
```

Preflight is read-only. It uses ordinary gcloud authentication and reports
blocker codes without logging auth errors or reading credential stores. A
successful billing preflight is necessary but insufficient for deployment:
IAM scope, free allowance, database and image storage still require inspection.

## Proposed first cloud deployment

Use a dedicated research project with Cloud Billing linked. A Google payment
method or Google AI Plus subscription alone is not linked Cloud Billing.

- Region `us-central1`; private Cloud Run service `kcp-cloud-shadow`.
- Request-based CPU billing, min instances 0, max instances 1, concurrency 1,
  1 CPU, 512 MiB RAM, request timeout 60 s. No GPU or always-on background loop.
- Dedicated service identity, Firestore access scoped to the research state.
  Separate Scheduler identity with only `run.invoker` on this service.
- Native-mode Firestore and one authenticated OIDC Scheduler job `*/10 * * * *`.
  Application tick budget 160/day; 90 s fencing lease; at most 12 chain rows.
- Cloud Run environment `KCP_CLOUD_MODE=shadow`, `GOOGLE_CLOUD_PROJECT=<project>`.
  Cloud Run IAM must enforce authentication; app code does not implement a
  separate public token endpoint. Do not expose with `--allow-unauthenticated`.
- Docker build context must be this folder, **never the repository root**:
  `docker build -f cloud_orchestrator/Dockerfile cloud_orchestrator`.
  The context allowlist excludes every research file, credential, local state
  database, test artifact, image and experiment bundle.
- Check remaining registry/network/logging allowances, set retention, and keep
  only small summaries in Firestore. A daily tick budget is not a billing cap.

No cloud resource or Scheduler job is created by the Python service or preflight.
Deployment awaits billing setup, an authenticated CLI and scoped project/IAM.

## Durable state schema

`kcp_cloud_control/tick`: global lease owner, monotonically increasing fence,
lease expiry, UTC-day tick count. Firestore transactions fence overlapping or
late requests. This does not replace future per-account submission leases.

`kcp_cloud_chains/<id>`: approved sealed plan, state, exact remote observation,
native validation receipt. Plan includes source SHA per chunk, scientific SHA,
seed, max iterations, approved attempt cap, contiguous boundaries and explicit
no-test/no-Sheet flags. Never store a token or arbitrary command in a document.

`kcp_cloud_shadow/<id>`: small decision, reason, deterministic key, observation
time and fence. No raw request data, credentials, checkpoint or probability map.

Native validation receipts need all explicit checks in `REQUIRED_CHECKS`:
full checkpoint/model/optimizers/schedulers/RNG/history/source/split/metrics,
best and frozen weights, private full-SHA archive readback and recipient input
access. For a check that is inapplicable, the future native validator must
explicitly prove that fact; the coordinator never invents PASS values.

## Remaining before actual unattended continuation

1. Cloud deployment and real Firestore transaction/emulator integration test.
2. Secure user-authorized Kaggle auth onboarding (no desktop DPAPI export).
3. Bounded live exact-version status/quota/input reader; current release only
   consumes supplied observations and never refreshes their timestamps.
4. Portable, pinned native CPU validator/archive workflow on private Actions;
   parity on existing full artifacts and direct private Kaggle SHA readback.
5. Persistent validation-dispatch outbox, trusted result ingestion and exact
   run reconciliation, with least-privilege OIDC/workload identities.
6. Actual child source staging/submission adapter; transactional per-account
   leases, intent/receipt reconciliation and exact remote existence proof.
7. Shadow parity against live local owner; explicit lease-fenced ownership
   handover for one approved chain and PC-off end-to-end acceptance test.

The CPU fixture IDs/hashes in the tests are synthetic and not research results.
No new experiment, cap change, test access or credential migration is authorized
by any fixture, example, config or runtime document.

## 12 November: selected desktop sessions and read-only monitoring

`python -m desktop_app.cloud_connection --accounts <exact KCP IDs> --out plan.json`
creates a public metadata plan without accessing stored auth. Applying that
reviewed plan with `--apply-plan`, `--confirm-plan-sha` and `--receipt` invokes
the normal desktop credential manager in-process. It writes directly to the
selected project's private Secret Manager, verifies full readback, and prints
only receipt metadata. It does not refresh/save desktop sessions, start a
scheduler, expose credential-returning APIs, or save plaintext auth files.
Google CLI/ADC authorization is separate from Kaggle; no per-account fresh
Kaggle login is needed unless its existing permission was revoked.

Up to eight account bindings fit two vaults, four per vault. Initial enrollment
creates one version each; the maximum intended retention is two per vault.
An existing vault or uncertain intent blocks new writes. A verified restart
checks version metadata without loading desktop credentials again. Cloud Run
receives only pinned secret-version names and public plan hashes via
`KCP_CLOUD_VAULTS`. Its identity needs secretAccessor on those exact vaults;
it does not need project-wide secret access or secret creation/rotation rights.
OAuth access tokens refresh in memory through the official SDK; the SDK's
refresh token is unchanged, so hourly access refresh does not create versions.

Optional `kcp_cloud_monitors` rows contain exact account/notebook/version/source
SHA, never credentials. The official V1 API rejected version_label (404) and
slug/version (403) in a real read-only probe. The reader therefore verifies
current version, notebook ID, private visibility and source SHA before and
after the status RPC, failing closed on drift. This is source-fenced evidence,
not a claim that the server honored an explicit version parameter.

Read-only observations publish transactionally to `kcp_cloud_status` under the
same finite-tick fence. They do not grant cloud training ownership. Native
validation, archive/submission transport and single-writer handover remain
required before unattended training continuation can be claimed.

## Finite infrastructure coordinator (12 November)

The separately deployed `coordinator_service` enables only `cpu_fixture`:
an immutable, expiring plan containing exactly two private, offline, stdlib
CPU jobs. A Firestore transaction persists an account reservation and outbox
intent before the one SaveKernel POST. Incomplete responses are reconciled by
exact account/source/version/notebook lookup, never by another POST. The
second job mounts and validates the first job's output; the coordinator also
validates the output receipt before advancing. This fixture grants no training
ownership, extra scientific arms, seeds or GPU attempts.

The portable native validator separately passed an actual private Kaggle CPU
run against the original validated DF65 checkpoint, with full state/RNG/history,
source/split, frozen model and best-weight parity. `run_kernel_output` additionally
verifies all eight original files before and after native validation, honestly
labeling private kernel output readback rather than claiming a dataset archive.
The CPU-only `continuation.stage_child` changes at most six operational CFG
fields; it regenerated an already-running local DF65 chunk byte for byte.
These last two adapters are local preparation and are not enabled by the
deployed CPU-fixture coordinator.

Research dispatch still requires native result ingestion, durable archive/input
gates, fresh official account admission and an explicit single-writer handover.
Keep the original local scientific owners alive until that handover exists.

## Actual scientific handover: DF65 final tail

`fm_service` is a separate, private `df65_tail_v1` service. Its exact plan SHA
is pinned in deployment environment; it accepts only the existing seed1337
DF65 parent14400 and its original tail to15000. The three fixed stages are
native CPU14400, one T4 training tail, native CPU15000. No automatic retry or
extra seed/arm is permitted. It waits for positive official GPU quota and fewer
than two known active jobs; changed/unknown account notebooks or unreadable
inputs block new work. The parent kernel outputs stay private and receive full
eight-file SHA readback; this does not claim a separate dataset archive.

Activation requires an exact exclusive local-stop receipt, normal desktop
account dispatch disabled, and matching immutable plan. The desktop account
is not revoked: already-running Kaggle work remains active and monitored.
Local DF65 status STOP_CLOUD_HANDOVER denotes owner migration, not a result.
Do not restart its old controller or re-enable that account locally while the
cloud program owns it. Other scientific chains retain their existing owners.

CPU source rendering is canonical under Firestore map-key reordering. Each
stage has one durable outbox key independent of source formatting; source
drift after an uncertain request fails closed. A remote-source reconciliation
must adopt the existing version, never replay SaveKernel. Operator status uses
`python -m cloud_orchestrator.fm_admin status --root <public-plan-directory>`;
this is read-only and does not trigger a tick. Project LIVE_STATUS includes it.

The running services currently load only the vault group needed for Dat;
the second approved vault and all eight connections remain intact. Native
models are never loaded into the Cloud Run service or downloaded to the laptop
for this continuation; Kaggle CPU performs native validation.

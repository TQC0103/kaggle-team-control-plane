# Kaggle Team Control Plane

[![CI](https://github.com/TQC0103/kaggle-team-control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/TQC0103/kaggle-team-control-plane/actions/workflows/ci.yml)

Round-1 MVP for running experiments from one Windows computer across a flexible
registry of explicitly assigned, owner-consented Kaggle accounts. A human operator and
an agent share the same account registry, parallel queue, job state, logs,
downloaded artifacts, official Kaggle accelerator quotas, and audit history.

This is an orchestration layer, not a quota-bypass tool. Every experiment names
its owner account, retries remain on that account, and the scheduler never
silently rotates work to another owner. At most two jobs per account are active
locally, while jobs on different accounts can run in parallel. Round 1 caps one
batch and the local worker pool at ten jobs; it does not require ten accounts.

## What is included

- Python REST API, SQLite state, and a responsive local dashboard.
- Desktop dataset uploads, explicit private/public visibility, reader permissions,
  remote metadata inspection, and persistent background-operation history.
- One isolated Kaggle CLI environment and staging directory per job.
- Account onboarding with owner consent, pause, permanent revoke, and audit.
- No durable plaintext credentials: SQLite stores only an environment-variable
  name such as `KCP_KAGGLE_MEMBER_01`.
- Explicit batches of 1-10 experiments, status polling, results, retry, cancel,
  reconciliation, automatic official quota synchronization, paged log viewing,
  and direct log/result downloads.
- A dependency-free MCP-style agent bridge with seven tools.
- A credential-free fake adapter and seeded 10-account/10-job demo.

## Requirements

### Dataset management

Open **Datasets → Upload dataset**, select an authenticated owner account, choose
files or a dedicated folder, and review its file count/size. Private is the default;
public exposure requires an explicit selection and confirmation. Readers can be
added by Kaggle username or through the account shortcuts. Nested folders become
ZIP archives via the Kaggle CLI. Uploading a new dataset never overwrites an
existing dataset or creates a new version of it.

For an existing dataset, enter `owner/slug` and select **Inspect & manage**. The
owner can edit title, description, visibility, and readers. Other metadata,
license, and writer/admin roles are preserved. KCP verifies the remote visibility
and permissions when Kaggle returns them; if a change is refused or cannot be
verified, the operation displays an error or a permission-verification notice.
Kaggle's omitted default fields are interpreted using its metadata contract
(public visibility and an empty collaborator list). Accounts with reader access
can inspect files/status, but only the selected owner can change settings.

Uploads run in the background, separate from GPU jobs; phase indicators describe
actual steps rather than estimating an upload percentage. Temporary staged copies
are removed after completion/failure. Original files are untouched. After an app
restart, an unfinished operation is marked **interrupted**; inspect the remote
dataset before a new submission. KCP does not automatically replay uploads.

Local API endpoints (same origin/auth rules as the other KCP API routes):

- `POST /api/datasets/prepare`: preview a dedicated `source_dir` inside the
  configured source root; returns an opaque `selection_id` and file manifest.
  The desktop native picker can explicitly select files outside that root.
- `POST /api/datasets/operations`: submit `kind: upload|settings`, `account_id`,
  `dataset_ref`, `title`, `description`, `is_private` (default true), `readers`,
  `idempotency_key`; uploads also need `selection_id` and an optional `license`
  (default `other`). Public operations require `confirm_public: true`.
  Returns HTTP 202 and an operation ID. Repeat the same idempotency key/request
  to retrieve the existing operation instead of uploading again.
- `GET /api/datasets/operations` and `GET /api/datasets/operations/{id}`:
  saved phase/status/error and uploaded source-file SHA256 values.
- `POST /api/datasets/inspect`: read metadata, file list, and Kaggle status using
  `account_id` and `dataset_ref`.

The existing `/api/datasets/private-checkpoint/*` routes remain compatible.
Credentials are resolved by KCP internally, never accepted in dataset payloads.
Credential/config files, links, protected directories and KCP's internal data
cannot be selected. Each selection is limited to 10,000 files / 100 GiB; one
dataset mutation per account runs at a time, with different accounts independent.

### Start with Windows and automatic artifact backup

In the installed Windows app, open **Local app settings → Start with Windows**.
This per-user toggle starts KCP at Windows sign-in, without administrator rights.
It can be turned off in the same place. A second launch exits without creating
another scheduler or archive worker.

Enable **Automatic private artifact backup** on the datasets page to back up
future completed job outputs. The persistent queue resumes discovery, interrupted
transfer reconciliation and full SHA-256 readback when KCP starts again. Original
local files are retained. The PC must be awake and KCP must remain running with
network access; this setting does not run uploads while the computer is off.
Separate research/SLURM relay scripts are not started by this desktop toggle.

### Runtime requirements

- Windows PowerShell 5.1 or PowerShell 7.
- Python 3.11 or newer.
- Node.js 22.13 or newer.
- For real runs: `python -m pip install --upgrade kaggle` and one current Kaggle
  API credential supplied by each participating owner.

## One-command demo (recommended first run)

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1 -Mode Demo -InstallDependencies
```

The launcher installs locked Node dependencies, starts the API and dashboard on
loopback, and seeds ten synthetic owners. Add `-VerifyDemo` when you explicitly
want it to wait for the full parallel smoke verification:

- 10 accounts exist;
- 10 jobs reached `succeeded`;
- the jobs used 10 distinct assigned accounts.

Open `http://127.0.0.1:3100`. Port 3100 is the launcher default because port
3000 is already occupied on this machine. Press Ctrl+C in the launcher
terminal to stop both processes. Logs and the isolated demo database/artifacts are under
`work/local-demo-<timestamp>/` and are git-ignored.

For an automated smoke run that verifies readiness and then cleans up:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1 -Mode Demo -ApiPort 18765 -DashboardPort 13000 -VerifyDemo -ExitAfterReady
```

If dependencies are already installed, omit `-InstallDependencies`.

## Real Kaggle setup on one Windows machine

The helper prompts with masked input. On Windows it can save encrypted copies
using DPAPI, so only the same Windows user on the same machine can decrypt them.
It never writes a plaintext token to `.env`, SQLite, or the repository.

The desktop app also supports per-member **Sign in with Kaggle** onboarding.
Kaggle's official OAuth page opens in the user's browser, so passwords and 2FA
never enter Control Plane. The returned refresh credential is kept only in the
Windows DPAPI store; access tokens are refreshed in memory and passed only to
the explicitly assigned job subprocess. The OAuth flow deliberately bypasses
Kaggle's default plaintext `~/.kaggle/credentials.json` persistence.

OAuth credentials are checked every minute and renewed five minutes before
expiry. Each CLI call reloads the latest token, including workers monitoring
long-running jobs. A per-account lock prevents competing refreshes; temporary
refresh errors back off automatically. Read-only commands that report expired
authentication force one refresh and retry once. Remote writes are never
replayed automatically. Revoked or expired refresh grants require reconnecting
the account; their public authentication state is `relogin_required`.

### Windows desktop app

The packaged desktop launches recurring Kaggle CLI polling and credential
inspection without creating visible console windows, so background status
refreshes do not steal focus or flash a terminal.

Run rows, account cards, and the run drawer consume the same backend-normalized
accelerator and elapsed-runtime fields. Completed jobs additionally surface an
allow-listed `runtime.json` summary (Python, Torch, CUDA, device, and model
library versions) when the downloaded artifact provides one.

While a kernel is queued or running, the scheduler also samples Kaggle's live
`kernels logs --follow` stream every 30 seconds (`KCP_LIVE_LOG_POLL_SECONDS`),
redacts credential values, and persists only bounded incremental updates. The open run drawer refreshes
automatically every 10 seconds and includes a manual **Refresh** button, so the
Kaggle browser page is no longer required for routine monitoring. A temporary
log-fetch failure is shown as a warning and does not fail the running job.

Build and install the native one-click shell once:

```powershell
.\scripts\build-desktop.ps1
.\scripts\install-desktop.ps1 -SourceRoot .\experiments
```

Then open **Kaggle Control Plane** from the Desktop or Start Menu. It starts the
local API and dashboard inside one app window, loads the existing DPAPI token
store, and shuts both services down when the window closes. **App settings** can
add, replace, or forget encrypted Kaggle tokens and select the experiment source
folder. Account/token/database changes are runtime data and never require a
frontend or EXE rebuild. Only code updates require rebuilding the app.

The dashboard includes first-run onboarding, run search, filtered CSV/JSON
exports, and confirmed bulk cancel/retry actions (capped at 10 runs per action).
Batch creation carries an idempotency key, so a browser retry cannot enqueue a
duplicate batch. **App settings** shows the embedded version/build, checks the
official GitHub releases feed on demand, and exports an allow-listed support
bundle containing aggregate diagnostics only.

To onboard a member without copying an API token, choose **Add member**, click
**Sign in with Kaggle**, and finish authentication in the browser. After Kaggle
returns to Control Plane, verify the detected owner, record explicit consent,
and choose **Connect account**. Manual token entry remains available as a
fallback in **App settings**.

For a shareable single-file installer that provisions Kaggle CLI when needed:

```powershell
.\scripts\build-installer.ps1
```

Send `release\installer\KaggleControlPlane-Setup.exe`. The recipient does not
need this repository or Node.js. Setup installs Python/Kaggle CLI automatically
when needed, so the first installation requires an internet connection.
Their DPAPI tokens, accounts, jobs, source folder, and results are created under
their own Windows profile and are never copied from the machine that built it.

Local early-beta builds are unsigned unless a signing certificate is supplied,
so Windows SmartScreen may show an **Unknown publisher** warning. Share the generated
`KaggleControlPlane-Setup.exe.sha256` file alongside the installer and have the
tester verify it before running Setup:

```powershell
(Get-FileHash .\KaggleControlPlane-Setup.exe -Algorithm SHA256).Hash
Get-Content .\KaggleControlPlane-Setup.exe.sha256
```

The two hashes must match exactly. Only accept an installer and checksum from a
project-owner-controlled channel; do not bypass a warning for an unexpected or
mismatched file.

### Release candidates

The manually dispatched **Release candidate** GitHub Actions workflow runs the
complete test suite on Windows, embeds the requested SemVer and commit SHA,
builds the installer, and uploads the installer plus SHA-256 checksum as a
14-day Actions artifact. Publishing a GitHub prerelease is an explicit workflow
input and defaults to off.

Authenticode signing is automatic when these repository secrets are configured:

- `WINDOWS_SIGNING_CERTIFICATE_BASE64`: base64-encoded code-signing PFX
- `WINDOWS_SIGNING_CERTIFICATE_PASSWORD`: PFX password

Without both secrets, the workflow clearly produces an unsigned candidate. It
never fabricates a signature. The checksum is regenerated after installer
signing so it always describes the distributed bytes.

```powershell
Set-ExecutionPolicy -Scope Process Bypass

New-Item -ItemType Directory -Force .\experiments | Out-Null
Copy-Item -Recurse -Force .\examples\kaggle-smoke-test .\experiments\kaggle-smoke-test

. .\scripts\set-team-credentials.ps1 -Count 2 -Persist
$credentialRefs = 1..2 | ForEach-Object { 'KCP_KAGGLE_MEMBER_{0:D2}' -f $_ }

.\scripts\start-local.ps1 `
  -Mode Real `
  -SourceRoot .\experiments `
  -CredentialRefs $credentialRefs `
  -InstallDependencies
```

`-InstallDependencies` is only needed on the first setup. Normal launches use
the compiled dashboard cache and avoid the slow first-page compilation. The
launcher rebuilds that cache automatically only after dashboard source files
change. Use `-DevDashboard` only while editing the UI, or
`-RebuildDashboard` to force a fresh production build.

After the first `-Persist`, `start-local.ps1` automatically loads the selected
credential references. Encrypted files live outside the repository under
`%LOCALAPPDATA%\KaggleControlPlane\credentials`. To encrypt credentials that
are already loaded in the current PowerShell without pasting them again:

```powershell
. .\scripts\set-team-credentials.ps1 -CredentialRefs $credentialRefs -SaveCurrent
```

Use one variable for one owner only. The value may be a current Kaggle API token
or legacy JSON such as
`{"username":"owner-kaggle-name","key":"legacy-key"}`. In the dashboard,
choose **Add member** and enter the exact variable name (for example
`KCP_KAGGLE_MEMBER_01`) in **Credential env ref**. Never paste the token into the
form. The backend validates a username included in legacy JSON against the
explicitly assigned Kaggle username.

Onboarding also records the consenting owner and a weekly-hours limit. This is
a local scheduling guard, not a claim about Kaggle's official allowance.

To clear only the current terminal values, or to delete the persisted copies:

```powershell
. .\scripts\set-team-credentials.ps1 -Count 2 -Clear

# Permanently remove the selected encrypted copies too.
. .\scripts\set-team-credentials.ps1 -Count 2 -Forget
```

### Create the first real batch

The source path entered in the batch composer must be under the `-SourceRoot`
used above, for example the absolute result of:

```powershell
(Resolve-Path .\experiments\kaggle-smoke-test).Path
```

Assign each experiment to its actual owner account. A kernel slug may be just
`family-smoke-01`; the control plane prefixes and validates the assigned Kaggle
username. The source field includes a local folder browser constrained to the
configured source root. GPU dispatch is pinned end-to-end to
`NvidiaTeslaT4`: the backend writes the shape into kernel metadata and passes it
to `kaggle kernels push --accelerator`, rather than relying on Kaggle's P100
default. TPU dispatch similarly uses `TpuV38`; CPU jobs omit a machine shape.
The API rejects unsupported or accelerator/shape-mismatched values.

Start with CPU smoke jobs before using accelerators.

The scheduler permits up to two simultaneous jobs per account and still caps a
batch at ten jobs. A third job for the same account stays queued until one of
that account's two active jobs finishes.

When connecting a new member, choose a credential reference and click **Detect
account**. For a current Kaggle API token, the backend asks Kaggle to validate
the token and infer the exact username; the dashboard uses that username as the
recommended owner name and consent name. The token itself is never returned to
the browser. Legacy credential JSON uses its embedded username.

Each source directory must contain `kernel-metadata.json`, exactly one top-level
`.py` or `.ipynb`, and no symlinks. The source is copied to per-job staging and
the original is never modified. The backend overwrites metadata owner/id and
accelerator fields for the assigned account and removes legacy `id_no`. Results
are downloaded only under the managed artifact root. See
`examples/kaggle-smoke-test/README.md` for the minimal workload.

## Safety semantics operators must know

### Cancel and remote reconciliation

- Cancelling a queued job is final locally.
- After submission starts, Kaggle CLI has no reliable public remote-stop
  operation. Cancel/revoke stops the local CLI or monitor, but the remote kernel
  may continue.
- Such a job has `remote_may_be_running=true`; its account pauses new batches,
  retries, and dispatch while the remote state is checked.
- Control Plane rechecks Kaggle automatically, including after desktop restart
  and after a local-only cancellation. The dashboard distinguishes a confirmed
  active remote kernel from a terminal local record awaiting confirmation; it
  never asks an operator to type a username to "unlock" either state. Scheduling
  resumes only after Kaggle reports a terminal result.

### Official Kaggle quota

- GPU and TPU used, remaining, total, and refresh time come only from Kaggle's
  authenticated quota API (`kaggle quota --format json`).
- The control plane synchronizes at startup, every five minutes, and after jobs
  finish. **Sync now** performs an immediate refresh for one account.
- CPU jobs never decrement GPU or TPU quota. No wall-time estimate is displayed
  or used for scheduling.
- If official quota is unavailable, the dashboard marks it unavailable/stale
  and blocks new GPU/TPU dispatch. CPU work remains eligible.
- A depleted resource blocks only that accelerator; for example, exhausted GPU
  quota does not block CPU work or available TPU quota.

### Disable versus revoke

- **Disable account** pauses new assignments and queued dispatch only. Work
  already active is allowed to finish.
- **Revoke access** is permanent in the MVP: it removes the credential reference,
  blocks all future work, and requests a strong local stop for active monitors.
  Because remote stop is not guaranteed, check Kaggle afterward.

All HTTP audit actors are server-derived (`local-client` or
`authenticated-client`); caller-supplied actor headers are ignored.

## Agent interface

With the control plane running:

```powershell
$env:KAGGLE_TEAM_API_URL = "http://127.0.0.1:8765"
python -m agent_interface serve
```

Available tools are `list_accounts`, `submit_batch`, `list_runs`, `cancel_run`,
`retry_run`, `fetch_result`, and `audit_events`. Each submitted experiment must
name an `account_id`; quota values in account responses are official Kaggle data.

Direct checks:

```powershell
python -m agent_interface tools
python -m agent_interface call list_accounts
```

If a bearer token protects the API, also set `KAGGLE_TEAM_API_TOKEN` for the
agent process. See `agent_interface/mcp-config.example.json`.

## REST API

```text
GET   /api/health
GET   /api/accounts
POST  /api/accounts
GET   /api/accounts/{id}
PATCH /api/accounts/{id}
POST  /api/accounts/{id}/revoke
POST  /api/accounts/{id}/reconcile     {"confirmed":true,"note":"..."}
POST  /api/accounts/{id}/quota/sync
GET   /api/batches
POST  /api/batches
GET   /api/batches/{id}
GET   /api/jobs
GET   /api/jobs?status=submitted&limit=20&summary=1
GET   /api/jobs/stats
GET   /api/jobs/{id}
GET   /api/jobs/{id}?include_remote_logs=0&event_limit=100
POST  /api/jobs/{id}/cancel
POST  /api/jobs/{id}/retry
GET   /api/jobs/{id}/result
GET   /api/jobs/{id}/events?before_id=...&limit=200
GET   /api/jobs/{id}/logs/download
GET   /api/jobs/{id}/result/download
GET   /api/audit
```

Run details render live remote output and Control Plane events in bounded chunks
and page older events on demand. For failed kernels, the scheduler downloads Kaggle diagnostics automatically; the
log endpoint also fetches them on demand for failures created before this
feature existed. Credential values are redacted from downloaded text artifacts,
and the UTF-8 log combines Control Plane events with bounded remote `.log`
content. A successful result download is a streamed ZIP containing
`job-result.json` plus every managed artifact downloaded from Kaggle.

## Network and credential boundary

The supplied launcher binds API and dashboard to `127.0.0.1` and deliberately
does not expose a browser bearer token. Binding the backend beyond loopback
requires `KCP_API_TOKEN`; the current dashboard does not attach that token, so
remote/team-network deployment needs an authenticated reverse proxy in a later
round. Do not port-forward this MVP or place it on an untrusted LAN.

Only credential references are stored. Credential values live in the backend's
environment and are resolved just in time into a minimal child environment for
the assigned Kaggle CLI subprocess. The launcher removes those named values
before starting the dashboard so they are not inherited by Node/Vite.

### Dataset inventory and research archives (desktop beta.9)

The Datasets panel lists owned datasets across connected accounts, remote sizes, visibility when inspected, account sync/errors, upload operations, imported SHA-256 readback reports, and whether an archive's local copy was removed. Search, account filtering and size/date sorting use the cached inventory; **Sync Kaggle inventory** performs an asynchronous remote scan with two workers and pagination. Shared inputs are not claimed as owned. Failed account refreshes retain the previous inventory and show the error.

Dataset deletion requires the exact owning account, typing `owner/slug`, and an explicit confirmation. A locally evicted archive requires acknowledgement that the archived copy is being removed. Active job inputs and active upload/settings operations block deletion; new jobs/retries cannot race a pending or unresolved deletion. The official `kaggle datasets delete owner/slug --yes` command is issued once. Only an owner metadata lookup returning 404 marks deletion confirmed; 403/auth/network errors stay uncertain and expose a read-only status check. No real dataset is deleted by tests or installation.

Inventory API: `GET /api/datasets/library`, `POST /api/datasets/library/refresh`. Archive runners register `POST /api/datasets/library/verification` with `account_id`, `dataset_ref`, original upload `operation_id`, `method=full_remote_readback`, `private_verified=true`, full `files=[{name,bytes,sha256}]`, and `local_evicted`. Every reported hash/size must match the uploaded manifest. Register before eviction and update after verified eviction; upload success alone is not checksum verification. Observed remote updates invalidate an older report. These are imported readback reports, not an independent server readback service.

Delete API: `POST /api/datasets/library/delete` with an idempotency key and exact confirmation; `POST /api/datasets/library/deletion-status` is read-only reconciliation. The MCP plugin exposes the corresponding inventory, sync, delete and reconciliation tools. Remote deletion is never inferred from a timeout or automatically replayed after restart.

The cache lives under KCP runtime storage, separate from credentials and uploaded data. Deleting a remote dataset does not delete unrelated local files or restore the space already moved off the laptop.

## Verification

```powershell
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\scripts\start-local.ps1), [ref]$null, [ref]$errors) | Out-Null
if ($errors.Count) { $errors; exit 1 }

python -m py_compile scripts\demo_server.py scripts\verify_demo.py
python -m unittest discover -s tests_backend -v
python -m unittest discover -s tests_desktop -v
python -m unittest discover -s agent_interface/tests -v
python -m pytest plugins\kaggle-control-plane\scripts\test_server.py -q
npm run lint
npm test
```

Pull requests and pushes to `main` run the same checks on GitHub Actions for
Windows, the supported desktop platform.

Real Kaggle execution still depends on each account's current limits,
accelerator availability, network health, and Kaggle service behavior.

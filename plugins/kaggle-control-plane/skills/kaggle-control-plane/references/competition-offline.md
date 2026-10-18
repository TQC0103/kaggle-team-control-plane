# Competition accelerators and offline runtimes

Use this reference for advanced GPU requests or notebooks without Internet.
Keep existing research runs on their preregistered shape and package pins.

## Establish what is available

`kcp_accelerator_catalog` / `GET /api/accelerators/catalog` lists supported IDs;
it does not establish account access. `kcp_competition_access(account_id)` /
`GET /api/accounts/{id}/competitions` lists already-entered competitions through
the vault boundary. Neither operation joins a competition or accepts rules.
The bounded first page reports `truncated` if more discovery is needed.

Official CLI IDs: `NvidiaTeslaT4`, `NvidiaL4`, `NvidiaL4X1`,
`NvidiaTeslaA100`, `NvidiaH100`, `NvidiaRtxPro6000`.
Some are competition-specific or admin-only. No generic GPU fallback.
Membership, a local certificate, and a JSON profile are not evidence that
Kaggle granted a particular accelerator or approved unrelated research.
Do not bulk-join competitions to acquire quota, rotate accounts to bypass
limits, or attach an unrelated competition solely to unlock its hardware.

Check the actual competition's rules and account access first. Gemma 4 and ARC
2026 advertise L4 ×4, require Internet off, restrict use to attached notebooks,
and currently charge twice the ordinary GPU quota (subject to change).
Four 24 GB cards are separate devices, not a single 96 GB allocation. Use DDP
or explicit model sharding to use multiple cards; a single CUDA process may
still use just one. Do not promise faster training before measuring it.

Sources, checked 2026-10-19:
- https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md
- https://www.kaggle.com/competitions/gemma-4-developer-agent/overview
- https://www.kaggle.com/competitions/arc-prize-2026-arc-agi-2/overview/upgraded-accelerators

## Prepare a new offline bundle

App source contains `scripts/package-offline-job.py`. It emits one self-contained
`run.py`, `kernel-metadata.json`, and `offline-profile.json` into a new directory.
Kaggle push uploads the selected entrypoint, not arbitrary companion scripts.
Do not rely on local helper files appearing remotely.

Example from the app checkout (use the actual reviewed competition/account):

```powershell
python scripts/package-offline-job.py --source experiments/offline-smoke-v1 --workload C:/explicit/workload.py --owner OWNER --slug offline-smoke-v1 --shape NvidiaL4 --competition COMPETITION --rules-url https://www.kaggle.com/competitions/COMPETITION/rules --minutes 5 --quota-multiplier 2
```

For a CPU infrastructure smoke, omit competition, use `--shape CPU`, and omit
machine_shape in the batch request. No GPU entitlement is needed for that test.

Two runtime strategies:

- **native** (default): an isolated job venv reads the Kaggle image's installed
  packages without pip installation. Good for capability smoke tests. For a
  production run lock `--python-abi cp313` (actual ABI) and
  `--expected-packages '{"torch":"EXACT_VERSION"}'`. Missing/mismatched pins fail.
- **wheelhouse**: build all Linux wheels ahead of time for the target Python ABI
  and CUDA/runtime combination, with a fully pinned requirements.lock using
  hashes. Create a JSON manifest `{"files":{"requirements.lock":"SHA256",
  "package.whl":"SHA256"}}`. Upload the wheelhouse as an accessible dataset.
  Add `--wheelhouse-manifest C:/path/unique-manifest.json
  --wheelhouse-dataset-ref owner/dataset --python-abi cp313` to the command.
  The runtime verifies the manifest and all files, then pip uses only local
  wheels with `--no-index --require-hashes --only-binary=:all:`.

The runner never downloads Python, virtualenv, wheels, models or repository
code. Native venv uses stdlib venv and does not need virtualenv.pyz. For legacy
research bootstrap, preserve its locked vendored zipapp and package pins; do
not silently switch its runtime to the native strategy.
Python ABI mismatch stops before installation. Bundling a portable alternative
Linux interpreter is **not implemented** in this runner. A study pinned to a
different Python version needs a separately validated offline runtime before
migration. Windows/macOS wheels are not usable Linux wheels.

Model weights, dataset files and checkpoints must be attached inputs with
recorded hashes. Replace pip URLs/git clones/Hugging Face downloads with those
local inputs. `enable_internet=false` is the platform network control; offline
environment variables alone are not network isolation.

## Preflight, submit, validate

The source profile records exact shape, reviewed competition attachment/rules
URL, purpose, expected session time, quota multiplier and runtime hashes. KCP
checks it at batch creation and staging, with no auto-fallback. The profile is
an integrity contract, not an entitlement certificate. Advanced competition
profiles currently support `purpose=competition_workload`; separately granted
unrelated research access requires a verified extension, not a false label.

An offline profile embeds the versioned runner. Regenerate a new bundle after
runner changes; old source stays immutable. Never rewrite historical bundles.

Before dispatch report expected wall time, charged quota = hours × multiplier,
account and exact shape. Keep the existing two-job/account cap and idempotency
key. A successful push alone is not submission proof: verify the remote kernel
exists/status is returned. Access denial stops the attempt; don't probe other
accounts or shapes as an automatic workaround.

First run a small bounded workload: enumerate actual GPUs/memory, inspect
Python/Torch/CUDA and architecture support, execute forward/backward, and write
runtime.json plus checksummed results. Download and validate the artifact.
Only call advanced GPU support remotely tested after that actual accelerator
passes. A CPU test proves offline packaging only.

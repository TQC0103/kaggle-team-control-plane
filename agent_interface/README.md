# Kaggle Team agent interface

Dependency-free MCP-style stdio and CLI bridge for the local control plane.
It never reads or stores Kaggle credentials; account secrets stay in the backend.
Use `mcp-config.example.json` as the launch configuration for an MCP-capable
coding agent, with the repository root as its working directory.

```powershell
$env:KAGGLE_TEAM_API_URL = "http://127.0.0.1:8765"
python -m agent_interface serve
```

For a direct smoke test:

```powershell
python -m agent_interface call list_accounts
python -m agent_interface call list_runs --arguments '{"status":"running"}'
python -m agent_interface call submit_batch --file batch.json
```

`batch.json` must include an explicit account for every experiment:

```json
{
  "name": "baseline sweep",
  "experiments": [
    {"account_id": "member-01", "experiment_name": "baseline", "source_dir": "experiments/baseline", "kernel_slug": "baseline-v1"},
    {"account_id": "member-02", "experiment_name": "augmentation", "source_dir": "experiments/augmentation", "kernel_slug": "augmentation-v1"}
  ]
}
```

Round 1 accepts at most 10 experiments per batch. A retry stays on the run's
original account; moving work to another owner requires a new explicit job.

Cancelling a queued run is final locally. Cancelling an already submitted run
only stops the local command/monitor because the Kaggle CLI cannot reliably
stop the remote kernel. The account is then blocked from new work until a human
checks Kaggle and uses the dashboard's reconciliation action. That safety gate
is intentionally not exposed as an agent tool.

Set `KAGGLE_TEAM_API_TOKEN` when the local control plane requires bearer auth.

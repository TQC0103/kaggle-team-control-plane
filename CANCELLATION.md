# Remote cancellation

Cancel now distinguishes a local queued request from a submitted Kaggle session.
`POST /api/jobs/<id>/cancel` accepts optional `kernel_session_id` (positive integer)
and `session_ref` (the exact `owner/slug`). The number must be the actual session
identity; it is not the notebook ID or notebook version. When omitted, KCP now
resolves it automatically through the official read-only output-download API.
It extracts the version from the original push receipt, verifies the exact
owner/slug and unchanged version using GetKernel, then requests that version's
output redirect with redirects disabled. Kaggle returns the actual session in
`https://api.kaggle.com/v1/kernels/output/download_zip/<session-id>`.
KCP validates this exact HTTPS host/path and closes the response without
downloading a ZIP or retaining signed URLs. No internal web API, notebook
deletion, replacement push, or guessed ID is involved. A changed/missing
submission version or an unrecognised redirect stops resolution safely.

The request authenticates only through the assigned account's existing vault and
calls the official SDK once with a bounded network timeout. A missing identity
or unresolved identity is reported without sending a mutation.
An accepted or ambiguous request remains nonterminal (`cancel_requested`) until
Kaggle status confirms termination. Repeated clicks do not replay an ambiguous
mutation. Read-only reconciliation preserves cancellation intent after restart.
Completion racing with cancellation remains success, not fabricated cancellation.

The drawer still accepts an optional explicit session ID; normal Cancel needs
no manual ID. The plugin tool accepts both optional fields,
and `python -m desktop_app.cancel_remote_job JOB_ID --session-id ID --session-ref
OWNER/SLUG` calls the same API. Omit those flags for automatic resolution. The
installed Kaggle CLI itself lacks a cancel command; KCP combines its existing
account authentication boundary with the generated official SDK instead.

Validation: `python -m unittest tests_backend.test_remote_cancel -v`, backend and
desktop regression suites, desktop TypeScript check and web build/render checks.
No new GPU notebook is needed for these checks.

Public API identity limitation: https://github.com/Kaggle/kaggle-cli/issues/1169.
Checked against the installed kaggle 2.2.4 SDK and an owned private notebook:
GetKernel exposes notebook ID and currentVersionNumber, not kernelSessionId.
On20/10, output-redirect resolution was verified on an owned private running
notebook, and the subsequent cancel transitioned remotely through
CANCEL_REQUESTED to CANCEL_ACKNOWLEDGED. The earlier missing-ID limitation is
resolved by this additional public endpoint, not by interpreting version as ID.

# Remote cancellation

Cancel now distinguishes a local queued request from a submitted Kaggle session.
`POST /api/jobs/<id>/cancel` accepts optional `kernel_session_id` (positive integer)
and `session_ref` (the exact `owner/slug`). The number must be the actual session
identity, explicitly bound by the operator; it is not the notebook ID or notebook
version. The generated Kaggle SDK has `CancelKernelSession`, but the public CLI
does not resolve this ID from a notebook slug. Public `GetKernel` and status
responses are insufficient. No internal web API, notebook deletion, or guessed
ID is used as a fallback.

The request authenticates only through the assigned account's existing vault and
calls the official SDK once with a bounded network timeout. A missing identity
returns `remote_cancellation_requires_session_id` without sending a mutation.
An accepted or ambiguous request remains nonterminal (`cancel_requested`) until
Kaggle status confirms termination. Repeated clicks do not replay an ambiguous
mutation. Read-only reconciliation preserves cancellation intent after restart.
Completion racing with cancellation remains success, not fabricated cancellation.

The drawer accepts an optional session ID, the plugin tool accepts both fields,
and `python -m desktop_app.cancel_remote_job JOB_ID --session-id ID --session-ref
OWNER/SLUG` calls the same API. The installed Kaggle CLI alone cannot implement
slug-based cancellation. This limitation must be reported rather than claiming a
session was stopped.

Validation: `python -m unittest tests_backend.test_remote_cancel -v`, backend and
desktop regression suites, desktop TypeScript check and web build/render checks.
No new GPU notebook is needed for these checks.

Public API identity limitation: https://github.com/Kaggle/kaggle-cli/issues/1169.
Checked against the installed kaggle 2.2.4 SDK and an owned private notebook:
GetKernel exposes notebook ID and currentVersionNumber, not kernelSessionId.

# Changes

## 2026-11-14

- Add a per-user **Start with Windows** toggle to desktop settings. Registration
  is verified against Windows, disabled startup entries can be restored by an
  explicit toggle, and a session-local mutex prevents duplicate desktop workers.
- Add persistent automatic private artifact backup, completed-output recovery,
  full remote SHA-256 readback, restore metadata, and an archive status panel.
  Reopening KCP resumes the saved queue; originals are retained.
- Improve exact-version restart reconciliation, recovered account-slot counting,
  private dataset settings, quota-warning parsing, and hidden CLI shutdown.
- Add cloud shadow/monitor/coordinator sources, private-vault onboarding, native
  checkpoint validation, and a deliberately scoped finite research continuation.
  Cloud training ownership remains explicitly fenced; desktop archive uploads
  have not been migrated to a cloud service.
- Synchronize the KCP plugin's archive tools and private-storage guidance.
- Include backend, desktop, cloud and plugin regression fixtures. Lint ignores
  generated local outputs; injected cloud clients do not require a real SDK.

The branch also contains the preceding remote-session cancellation fixes,
verified offline accelerator profiles, and the owned dataset inventory with
explicit verified deletion. Local credentials, runtime databases, artifacts,
generated experiment bundles and one-off research scripts are not release code.

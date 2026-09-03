# Desktop app and plugin maintenance

Read this reference only for Control Plane UI, OAuth, live-log, packaging,
startup, or plugin-maintenance tasks.

## Known local locations

Verify each path before acting because the checkout may move:

- app source: `C:\Users\ASUS\Documents\Codex\2026-08-13\t`
- installed EXE:
  `C:\Users\ASUS\AppData\Local\Programs\KaggleControlPlane\KaggleControlPlane.exe`
- plugin source:
  `C:\Users\ASUS\plugins\kaggle-control-plane`
- marketplace: `C:\Users\ASUS\.agents\plugins\marketplace.json`
- API/dashboard ports: `8765` / `3100`

Do not edit a versioned plugin cache as source of truth. Update the marketplace
source, validate it, apply one cachebuster, then reinstall. Start a new Codex
task to load the refreshed skill/tools.

## Startup and Not Responding triage

1. Check the exact installed process path, ports, and bounded requests to
   `/api/health`, `/api/accounts`, and `/api/jobs`. `Responding=false` alone is
   insufficient; API responsiveness is the stronger signal.
2. If API/UI hangs, stop only the exact installed executable and run the source
   entry point in a console once to capture pywebview errors. Do not repeatedly
   restart without new evidence.
3. A known freeze came from public `DesktopBridge.runtime`: pywebview recursively
   inspected `runtime.window.native` and WebView2 COM objects. Keep it private as
   `DesktopBridge._runtime`; expose only bridge methods.
4. Modal lag also came from an incorrect wrapper class and full-screen
   `backdrop-filter`. Use defined `.modal-layer`/`.drawer-layer` classes and
   avoid full-window blur.
5. Respect an explicit request not to reopen or retest after installation.
   Build/install only and state that final runtime verification was skipped.

## Windows children and live logs

- Every recurring Kaggle CLI subprocess must use `CREATE_NO_WINDOW` and hidden
  `STARTUPINFO` on Windows: polling, quota/identity inspection, submission,
  diagnostics, downloads, and bounded `--follow` log snapshots.
- Keep remote log reads bounded and incremental. Never let a log subprocess
  block the scheduler indefinitely.

## OAuth boundary

- Password and 2FA belong only in Kaggle's browser. Never add password entry to
  Control Plane.
- Kaggle's public OAuth `authenticate()` persists plaintext credentials. Capture
  the official flow result before that write, encrypt the refresh bundle with
  Windows DPAPI, and place only the current access token in process/job memory.
- Import `KaggleClient`, `KaggleEnv`, and OAuth types directly from `kagglesdk`.
  Importing full `KaggleApi` in the desktop path made PyInstaller collect Torch,
  SciPy, Pandas, and other unnecessary packages.

## Build and install

From the app source directory:

```powershell
.\scripts\build-desktop.ps1 -SkipWebBuild
.\scripts\install-desktop.ps1 -SourceRoot .\experiments
```

Use `-SkipWebBuild` only when `dist/client` already reflects frontend changes.
Before install, confirm no active local workload would be interrupted. Never
commit releases, build directories, databases, credentials, experiment bundles,
or runtime outputs.

Run proportional local checks before packaging: desktop/backend tests, frontend
lint/build test, compile check, and `git diff --check`. Inspect PyInstaller
warnings for Kaggle SDK imports and record the installed executable hash. Batch
related fixes and package once instead of rebuilding after each edit.

Release-candidate builds must embed the requested SemVer and commit SHA. Keep
GitHub prerelease publication opt-in. If Authenticode certificate secrets are
absent, identify the artifact as unsigned; never simulate a signature. Generate
or refresh the SHA-256 sidecar after the final signing step.

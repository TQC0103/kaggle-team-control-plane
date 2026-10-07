"""Package verification and immutable staging; does not install or start real jobs."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(r"C:\Users\ASUS\Documents\Codex\2026-09-17\t-d\outputs\kcp-auth-refresh-20261002")
OUT.mkdir(exist_ok=True, parents=True)
release = ROOT / "release" / "KaggleControlPlane"
with tempfile.TemporaryDirectory(prefix="kcp-isolated-smoke-") as temporary:
    smoke = Path(temporary)
    config_root = smoke / "KaggleControlPlane"
    config_root.mkdir()
    (config_root / "desktop-config.json").write_text(json.dumps({
        "source_root": str(smoke / "experiments"),
        "legacy_database": str(smoke / "absent.sqlite3"),
    }))
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(smoke)
    result = subprocess.run([str(release / "KaggleControlPlane.exe"), "--smoke",
                             "--api-port", "18765", "--dashboard-port", "13100"],
                            cwd=smoke, env=environment, timeout=45,
                            capture_output=True, creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError("Isolated packaged smoke check failed")
stage = OUT / "0.2.0-beta.3" / "KaggleControlPlane"
shutil.copytree(release, stage, dirs_exist_ok=True)
def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
sources = ["desktop_app/auth_refresh.py", "desktop_app/oauth.py", "desktop_app/main.py",
           "control_plane/credentials.py", "control_plane/adapters.py", "control_plane/service.py",
           "tests_desktop/test_auth_refresh.py", "scripts/install_desktop_when_idle.py"]
manifest = dict(version="0.2.0-beta.3", build_sha=subprocess.check_output(
    ["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, text=True).strip(),
    source_worktree_dirty=True, created_at=datetime.now(timezone.utc).isoformat(),
    packaged_isolated_smoke="PASS", authenticode="unsigned",
    source_files_sha256={p: sha(ROOT / p) for p in sources},
    release_files_sha256={p.relative_to(stage).as_posix(): sha(p) for p in stage.rglob("*") if p.is_file()})
(OUT / "release-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
(OUT / "KaggleControlPlane.exe.sha256").write_text(
    manifest["release_files_sha256"]["KaggleControlPlane.exe"] + "  KaggleControlPlane.exe\n", encoding="ascii")
print(json.dumps(dict(packaged_isolated_smoke="PASS", version=manifest["version"],
                     exe_sha256=manifest["release_files_sha256"]["KaggleControlPlane.exe"],
                     stage=str(stage), file_count=len(manifest["release_files_sha256"]))))

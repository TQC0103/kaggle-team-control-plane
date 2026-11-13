"""Source-only deployment bundle with a strict allowlist; never package the repo."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

SOURCE_FILES = (
    "__init__.py", "protocol.py", "storage.py", "service.py", "requirements.txt",
    "Dockerfile", ".dockerignore", ".gcloudignore", "cloudbuild.yaml",
    "deploy_shadow.sh", "README.md", "shadow-service.yaml", "connection.py", "kaggle_monitor.py",
    "transport.py", "cpu_fixture.py", "coordinator.py", "coordinator_service.py",
    "fm_tail.py", "fm_transport.py", "fm_service.py", "native_checkpoint.py", "continuation.py",
)


def package(source: Path, destination: Path):
    root = source.resolve(strict=True)
    content = {}
    for name in SOURCE_FILES:
        requested = root / name
        if requested.is_symlink():
            raise ValueError("source symlinks are not permitted")
        path = requested.resolve(strict=True)
        if path.parent != root or not path.is_file():
            raise ValueError("source file must remain inside the allowlisted folder")
        content[name] = path.read_bytes()
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    content["SOURCE_MANIFEST.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for name, data in sorted(content.items()):
                    entry = tarfile.TarInfo("kcp-cloud-shadow/" + name)
                    entry.size = len(data)
                    entry.mtime = entry.uid = entry.gid = 0
                    entry.mode = 0o755 if name.endswith(".sh") else 0o644
                    archive.addfile(entry, io.BytesIO(data))
    return {"archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "source_manifest_sha256": hashlib.sha256(content["SOURCE_MANIFEST.json"]).hexdigest(),
            "files": len(content), "bytes": destination.stat().st_size}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(package(Path(__file__).parent, args.out), indent=2))


if __name__ == "__main__":
    main()

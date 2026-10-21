"""Dataset uploads and metadata operations. Credentials remain in KCP's vault."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .dataset_transfer import _account_cli, _REF
from .errors import ConflictError, NotFoundError, ValidationError

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
_BLOCKED = {".git", ".ssh", ".codex", ".kaggle", "vault", "credentials", "kaggle.json",
            "credentials.json", "access_token", "desktop-config.json", "dataset-metadata.json"}
_LICENSES = {"other", "CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "ODC-BY-1.0", "ODbL-1.0"}
_TERMINAL = {"succeeded", "failed", "interrupted"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_file(path: Path):
    # Examine the original path before resolving: resolving first hides symlinks.
    for part in (path, *path.parents):
        name = part.name.casefold()
        if part.is_symlink() or name in _BLOCKED or name == ".env" or name.startswith(".env."):
            raise ValidationError("Selection contains a link, credential/config file, or protected directory")
    if not path.is_file():
        raise ValidationError("Select regular files only")


class DatasetManager:
    def __init__(self, service, cli_provider=None):
        self.service = service
        self.root = service.data_dir / "datasets"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._selections = {}
        self._operations = {}
        self._closed = False
        self._cli_provider = cli_provider or (lambda account_id: _account_cli(service, account_id))
        for path in self.root.glob("operation-*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] not in _TERMINAL:
                record.update(status="interrupted", phase="Review remote state before submitting again",
                              error="App stopped during this operation; it was not replayed automatically.", updated_at=_now())
                self._save(record)
            self._operations[record["id"]] = record

    def close(self):
        with self._lock:
            self._closed = True

    def _save(self, record):
        path = self.root / f"operation-{record['id']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _update(self, operation_id, **changes):
        with self._lock:
            record = self._operations[operation_id]
            record.update(changes, updated_at=_now())
            self._save(record)

    def list_operations(self):
        with self._lock:
            return [dict(r) for r in sorted(self._operations.values(), key=lambda r: r["created_at"], reverse=True)]

    def get_operation(self, operation_id):
        with self._lock:
            if operation_id not in self._operations:
                raise NotFoundError("dataset operation not found")
            return dict(self._operations[operation_id])

    def select(self, paths, *, folder=False):
        """Called by the trusted native picker, or scoped source-root preparation."""
        if not paths or (folder and len(paths) != 1):
            raise ValidationError("Select files or one folder")
        entries = []
        if folder:
            root = Path(paths[0]).absolute()
            if not root.is_dir() or root.is_symlink():
                raise ValidationError("Select a regular folder")
            # Do not recurse through linked/protected directories, even when empty.
            for directory, dirs, files in os.walk(root, followlinks=False):
                parent = Path(directory)
                for child in dirs:
                    candidate = parent / child
                    if candidate.is_symlink() or child.casefold() in _BLOCKED:
                        raise ValidationError("Folder contains a protected directory or link; select a dedicated data folder")
                for filename in files:
                    entries.append((parent / filename, (parent / filename).relative_to(root).as_posix()))
                    if len(entries) > 10000:
                        raise ValidationError("Select at most 10,000 files")
        else:
            entries = [(Path(p).absolute(), Path(p).name) for p in paths]
        if not entries or len(entries) > 10000:
            raise ValidationError("Select between 1 and 10,000 files")
        total = 0
        names = set()
        manifest = []
        for path, name in entries:
            _safe_file(path)
            if path.resolve().is_relative_to(self.service.data_dir.resolve()):
                raise ValidationError("KCP internal data cannot be uploaded; copy research files into a dedicated folder")
            if name.casefold() in names:
                raise ValidationError("Selection contains duplicate file names")
            names.add(name.casefold())
            stat = path.stat()
            total += stat.st_size
            manifest.append({"path": str(path), "name": name, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        if total > 100 * 1024**3 or total == 0:
            raise ValidationError("Selection must contain data and be at most 100 GiB")
        selection_id = uuid.uuid4().hex
        with self._lock:
            self._selections[selection_id] = manifest
        return {"selection_id": selection_id, "file_count": len(manifest), "bytes": total,
                "files": [{"name": f["name"], "bytes": f["bytes"]} for f in manifest[:100]],
                "nested_folders_zipped": any("/" in f["name"] for f in manifest)}

    def prepare(self, payload):
        source = Path(payload.get("source_dir", "")).absolute()
        if source.resolve() == self.service.allowed_source_root or not source.resolve().is_relative_to(self.service.allowed_source_root):
            raise ValidationError("Source must be a dedicated folder inside the configured source root")
        return self.select([source], folder=True)

    def _metadata(self, payload, account):
        ref = payload.get("dataset_ref", "")
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise ValidationError("Dataset reference must be owner/slug (slug: 6–50 letters, digits, dashes)")
        if ref.split("/")[0].casefold() != account["kaggle_username"].casefold():
            raise ValidationError("Choose the dataset owner's account to upload or edit it")
        private = payload.get("is_private", True)
        if not isinstance(private, bool):
            raise ValidationError("is_private must be true or false")
        if not private and payload.get("confirm_public") is not True:
            raise ValidationError("Explicit confirmation is required to make a dataset public")
        title = payload.get("title", "")
        if not isinstance(title, str) or not 6 <= len(title.strip()) <= 50:
            raise ValidationError("Title must be 6–50 characters")
        description = payload.get("description", "")
        if not isinstance(description, str) or len(description) > 50000:
            raise ValidationError("Description must be at most 50,000 characters")
        readers = payload.get("readers", [])
        if not isinstance(readers, list) or len(readers) > 100 or any(not isinstance(r, str) or not _NAME.fullmatch(r) for r in readers):
            raise ValidationError("Readers must be valid Kaggle usernames (at most 100)")
        # An existing license outside the curated creation list is preserved on edit.
        license_name = payload.get("license", "other")
        if payload.get("kind") == "upload" and license_name not in _LICENSES:
            raise ValidationError("Unsupported license; choose a listed license")
        return {"id": ref, "title": title.strip(), "description": description, "isPrivate": private,
                "licenses": [{"name": license_name}],
                "collaborators": [{"username": r, "role": "READER"} for r in sorted(set(readers)) if r.casefold() != account["kaggle_username"].casefold()]}

    def submit(self, payload, actor):
        self.service._reject_secrets(payload)
        kind = payload.get("kind")
        if kind not in {"upload", "settings"}:
            raise ValidationError("kind must be upload or settings")
        account_id = payload.get("account_id")
        account = self.service.database.get_account(account_id)
        if account["state"] != "enabled" or not account.get("credential_env_ref"):
            raise ValidationError("Choose an enabled authenticated account")
        metadata = self._metadata(payload, account)
        key = payload.get("idempotency_key", "")
        if not isinstance(key, str) or not 8 <= len(key) <= 120:
            raise ValidationError("idempotency_key must be 8–120 characters")
        with self._lock:
            if self._closed:
                raise ConflictError("App is closing; try again after reopening")
            library = getattr(self.service, 'dataset_library', None)
            if library and library.blocks_mutation(account_id, metadata['id']):
                raise ConflictError('Dataset deletion is in progress or unresolved; check its remote state first')
            previous = next((r for r in self._operations.values() if r["idempotency_key"] == key), None)
            # The same selection token remains valid on a client retry; persisted
            # retries are checked before accessing the in-memory selection.
            specification = {"kind": kind, "account_id": account_id, "metadata": metadata,
                             "selection_id": payload.get("selection_id") if kind == "upload" else None}
            request_hash = hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ConflictError("Idempotency key already belongs to a different request")
                return dict(previous)
            if any(r["account_id"] == account_id and r["status"] not in _TERMINAL for r in self._operations.values()):
                raise ConflictError("This account already has a dataset operation in progress")
            if any(r["dataset_ref"].casefold() == metadata["id"].casefold() and r["status"] not in _TERMINAL for r in self._operations.values()):
                raise ConflictError("This dataset already has an operation in progress")
            files = self._selections.get(payload.get("selection_id")) if kind == "upload" else []
            if kind == "upload" and not files:
                raise ValidationError("Select files again before uploading")
            operation_id = uuid.uuid4().hex
            record = {"id": operation_id, "kind": kind, "account_id": account_id, "dataset_ref": metadata["id"],
                      "title": metadata["title"], "is_private": metadata["isPrivate"], "readers": [r["username"] for r in metadata["collaborators"]],
                      "status": "queued", "phase": "Preparing", "file_count": len(files), "bytes": sum(f["bytes"] for f in files),
                      "idempotency_key": key, "request_hash": request_hash, "created_at": _now(), "updated_at": _now()}
            self._operations[operation_id] = record
            self._save(record)
            threading.Thread(target=self._execute, args=(operation_id, metadata, files, actor), daemon=True,
                             name=f"kcp-dataset-{operation_id[:8]}").start()
            return dict(record)

    def _remote_metadata(self, run, ref, directory):
        directory.mkdir(parents=True, exist_ok=True)
        run(["datasets", "metadata", ref, "-p", str(directory)])
        remote = json.loads((directory / "dataset-metadata.json").read_text(encoding="utf-8-sig"))
        remote = remote.get("info") or remote
        # KaggleObject.to_json(ignore_defaults=True) omits false and empty
        # lists. DatasetInfo defaults are is_private=False, collaborators=[].
        remote.setdefault("isPrivate", False)
        remote.setdefault("collaborators", [])
        if not isinstance(remote["isPrivate"], bool) or not isinstance(remote["collaborators"], list):
            raise ValidationError("Kaggle returned invalid dataset metadata")
        return remote

    def inspect(self, payload):
        self.service._reject_secrets(payload)
        ref = payload.get("dataset_ref", "")
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise ValidationError("Dataset reference must be owner/slug")
        _, run = self._cli_provider(payload.get("account_id"))
        directory = self.root / "inspections" / uuid.uuid4().hex
        try:
            remote = self._remote_metadata(run, ref, directory)
            library = getattr(self.service, 'dataset_library', None)
            if library:
                library.record_metadata(payload.get('account_id'), ref, remote)
            # Return only UI metadata, never CLI environment or private configs.
            return {"dataset_ref": ref, "title": remote.get("title", ""), "description": remote.get("description", ""),
                    "is_private": remote.get("isPrivate"), "license": (remote.get("licenses") or [{"name": "other"}])[0]["name"],
                    "readers": [r["username"] for r in remote.get("collaborators", []) if r.get("role") == "READER"],
                    "files": run(["datasets", "files", ref, "--csv"]), "cli_status": run(["datasets", "status", ref]),
                    "readers_available": "collaborators" in remote}
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _execute(self, operation_id, metadata, files, actor):
        record = self.get_operation(operation_id)
        directory = self.root / "bundles" / operation_id
        try:
            self._update(operation_id, status="running", phase="Staging files")
            _, run = self._cli_provider(record["account_id"])
            directory.mkdir(parents=True, exist_ok=True)
            hashes = []
            for entry in files:
                path = Path(entry["path"])
                _safe_file(path)
                stat = path.stat()
                if stat.st_size != entry["bytes"] or stat.st_mtime_ns != entry["mtime_ns"]:
                    raise ValidationError("Selected file changed; select it again")
                target = directory / entry["name"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                with target.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                hashes.append({"name": entry["name"], "bytes": entry["bytes"], "sha256": digest})
            if record["kind"] == "settings":
                self._update(operation_id, phase="Reading current settings")
                old = self._remote_metadata(run, metadata["id"], directory / "before")
                # Visibility is always explicit. Preserve license and non-reader
                # roles/resources that are outside this editor's scope.
                readers = metadata.pop("collaborators")
                existing_roles = {r["username"].casefold() for r in old.get("collaborators", []) if r.get("role") != "READER"}
                if any(r["username"].casefold() in existing_roles for r in readers):
                    raise ValidationError("A requested reader already has writer/admin access; remove it from the reader field to preserve that role")
                title, description, private = metadata["title"], metadata["description"], metadata["isPrivate"]
                metadata = {**old, "id": metadata["id"], "title": title, "description": description,
                            "isPrivate": private, "collaborators": readers + [r for r in old.get("collaborators", []) if r.get("role") != "READER"]}
            (directory / "dataset-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            if record["kind"] == "upload":
                self._update(operation_id, phase="Uploading to Kaggle", files=hashes)
                args = ["datasets", "create", "-p", str(directory), "-q", "--dir-mode", "zip"]
                if not metadata["isPrivate"]:
                    args.append("--public")
                output = run(args, 3600)
                if re.search(r"(?:creation error|\berror\b|\bfailed\b)", output, re.I):
                    raise ValidationError(output[:1500])
            self._update(operation_id, phase="Applying visibility and reader permissions")
            output = run(["datasets", "metadata", metadata["id"], "--update", "-p", str(directory)], 180)
            if re.search(r"(?:update error|\berror\b|\bfailed\b)", output, re.I):
                raise ValidationError(output[:1500])
            self._update(operation_id, phase="Verifying remote settings")
            remote = self._remote_metadata(run, metadata["id"], directory / "verified")
            if remote.get("isPrivate") is not metadata["isPrivate"]:
                raise ValidationError("Remote visibility differs from the request; review dataset on Kaggle")
            requested = {r["username"].casefold() for r in metadata["collaborators"] if r["role"] == "READER"}
            observed = {r["username"].casefold() for r in remote.get("collaborators", []) if r.get("role") == "READER"}
            if "collaborators" in remote and requested != observed:
                raise ValidationError("Remote reader permissions differ from the request")
            cli_status = run(["datasets", "status", metadata["id"]])
            if re.search(r"\b(error|failed|failure)\b", cli_status, re.I):
                raise ValidationError("Kaggle reports a dataset processing failure: " + cli_status[:500])
            self._update(operation_id, status="succeeded", phase="Completed", visibility_verified=True,
                         readers_verified=True, cli_status=cli_status)
            self.service.database.append_audit(actor, "dataset." + record["kind"], "dataset", metadata["id"],
                                               {"operation_id": operation_id, "is_private": metadata["isPrivate"], "readers": sorted(requested)})
        except Exception as exc:
            # CLI errors have already been redacted by _account_cli.
            message = str(exc)[:1500] if isinstance(exc, ValidationError) else "Dataset operation failed; check file access and remote state"
            self._update(operation_id, status="failed", phase="Failed — review remote state before retrying", error=message,
                         error_type=type(exc).__name__)
        finally:
            # Staged files are disposable copies; original selected files survive.
            shutil.rmtree(directory, ignore_errors=True)

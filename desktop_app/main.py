"""One-click Windows desktop application for Kaggle Control Plane."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from control_plane.api import create_server
from control_plane.service import ControlPlaneService

from .credential_store import WindowsCredentialStore


APP_NAME = "Kaggle Control Plane"


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def app_data_root() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "KaggleControlPlane"


def load_config() -> dict[str, Any]:
    path = app_data_root() / "desktop-config.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    root = app_data_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "desktop-config.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def message_box(message: str, title: str = APP_NAME, error: bool = False) -> None:
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 if error else 0x40)


def migrate_legacy_database(destination: Path, legacy: Path | None) -> None:
    if destination.exists() or not legacy or not legacy.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy) as source, sqlite3.connect(destination) as target:
        source.backup(target)


class StaticDashboardHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        if self.path.startswith("/_next/static/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class DesktopBridge:
    def __init__(self, runtime: "DesktopRuntime"):
        self.runtime = runtime

    def get_settings(self) -> dict[str, Any]:
        return {
            "desktop": True,
            "source_root": str(self.runtime.source_root),
            "credential_refs": self.runtime.credential_store.list_refs(),
            "data_root": str(self.runtime.data_root),
            "restart_required": self.runtime.restart_required,
        }

    def save_credential(self, credential_ref: str, token: str) -> dict[str, Any]:
        try:
            ref = self.runtime.credential_store.save(credential_ref, token)
            os.environ[ref] = token
            self.runtime.refresh_credential_refs()
            return {"ok": True, "credential_ref": ref}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def forget_credential(self, credential_ref: str) -> dict[str, Any]:
        try:
            ref = self.runtime.credential_store.validate_ref(credential_ref)
            removed = self.runtime.credential_store.forget(ref)
            os.environ.pop(ref, None)
            self.runtime.refresh_credential_refs()
            return {"ok": True, "removed": removed}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_source_root(self) -> dict[str, Any]:
        try:
            import webview
            result = self.runtime.window.create_file_dialog(webview.FileDialog.FOLDER)
            if not result:
                return {"ok": False, "cancelled": True}
            selected = Path(result[0]).resolve()
            config = load_config()
            config["source_root"] = str(selected)
            save_config(config)
            self.runtime.restart_required = True
            return {"ok": True, "source_root": str(selected), "restart_required": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_data_folder(self) -> dict[str, Any]:
        try:
            os.startfile(self.runtime.data_root)  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


class DesktopRuntime:
    def __init__(self, api_port: int = 8765, dashboard_port: int = 3100):
        self.api_port = api_port
        self.dashboard_port = dashboard_port
        self.data_root = app_data_root() / "data"
        self.data_root.mkdir(parents=True, exist_ok=True)
        config = load_config()
        project_root = Path.cwd()
        configured_source = config.get("source_root")
        fallback_source = Path.home() / "Documents" / "KaggleControlPlane" / "experiments"
        self.source_root = Path(configured_source or fallback_source).expanduser().resolve()
        self.source_root.mkdir(parents=True, exist_ok=True)
        legacy_db_value = config.get("legacy_database")
        legacy_db = Path(legacy_db_value) if legacy_db_value else project_root / "data" / "control-plane.sqlite3"
        self.database_path = self.data_root / "control-plane.sqlite3"
        migrate_legacy_database(self.database_path, legacy_db)
        self.credential_store = WindowsCredentialStore()
        self.restart_required = False
        self.service: ControlPlaneService | None = None
        self.api_server = None
        self.dashboard_server: ThreadingHTTPServer | None = None
        self.threads: list[threading.Thread] = []
        self.window = None

    def refresh_credential_refs(self) -> None:
        refs = self.credential_store.list_refs()
        os.environ["KCP_CREDENTIAL_REFS"] = ",".join(refs)

    def load_credentials(self) -> None:
        refs = self.credential_store.list_refs()
        for ref in refs:
            os.environ[ref] = self.credential_store.load(ref)
        self.refresh_credential_refs()

    def start(self) -> None:
        for port in (self.api_port, self.dashboard_port):
            if not port_available(port):
                raise RuntimeError(
                    f"Port {port} is already in use. Close the old PowerShell launcher or app, then try again."
                )
        self.load_credentials()
        kaggle_cli = shutil.which("kaggle")
        if not kaggle_cli:
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / "Scripts" / "kaggle.exe",
                Path(os.environ.get("APPDATA", "")) / "Python" / "Python311" / "Scripts" / "kaggle.exe",
            ]
            kaggle_cli = next((str(path) for path in candidates if path.is_file()), None)
        if not kaggle_cli:
            raise RuntimeError("Kaggle CLI is missing. Run the Setup installer again while connected to the internet.")
        os.environ["KCP_KAGGLE_EXECUTABLE"] = kaggle_cli
        self.service = ControlPlaneService(
            self.database_path,
            data_dir=self.data_root / "runtime",
            allowed_source_root=self.source_root,
            adapter_name="kaggle",
            max_workers=10,
            max_jobs_per_account=2,
            quota_start_delay_seconds=8,
        )
        self.api_server = create_server(self.service, host="127.0.0.1", port=self.api_port)
        static_root = resource_root() / "dist" / "client"
        if not (static_root / "index.html").is_file():
            raise RuntimeError("The packaged dashboard is missing. Rebuild the desktop app.")
        handler = partial(StaticDashboardHandler, directory=str(static_root))
        self.dashboard_server = ThreadingHTTPServer(("127.0.0.1", self.dashboard_port), handler)
        self.dashboard_server.daemon_threads = True
        for name, server in (("kcp-api", self.api_server), ("kcp-dashboard", self.dashboard_server)):
            thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        for server in (self.dashboard_server, self.api_server):
            if server:
                server.shutdown()
                server.server_close()
        if self.service:
            self.service.close()
        for ref in self.credential_store.list_refs():
            os.environ.pop(ref, None)


def smoke_test(runtime: DesktopRuntime) -> int:
    runtime.start()
    try:
        for url in (
            f"http://127.0.0.1:{runtime.api_port}/api/health",
            f"http://127.0.0.1:{runtime.dashboard_port}/",
        ):
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    return 1
        print("desktop smoke test passed")
        return 0
    finally:
        runtime.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--dashboard-port", type=int, default=3100)
    args = parser.parse_args()
    runtime = DesktopRuntime(args.api_port, args.dashboard_port)
    if args.smoke:
        return smoke_test(runtime)
    try:
        runtime.start()
        import webview
        bridge = DesktopBridge(runtime)
        runtime.window = webview.create_window(
            APP_NAME,
            f"http://127.0.0.1:{runtime.dashboard_port}/",
            js_api=bridge,
            width=1440,
            height=920,
            min_size=(980, 680),
            background_color="#f3f1e8",
        )
        webview.start(gui="edgechromium", debug=False, private_mode=False)
        return 0
    except Exception as exc:
        message_box(str(exc), error=True)
        return 1
    finally:
        runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())

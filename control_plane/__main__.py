"""Run with: python -m control_plane"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .api import create_server
from .service import ControlPlaneService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kaggle Team Control Plane MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--db",
        default=os.environ.get("KCP_DB_PATH", "control-plane.sqlite3"),
        help="SQLite path (or set KCP_DB_PATH)",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("KCP_DATA_DIR"),
        help="staging/artifact root (or set KCP_DATA_DIR)",
    )
    parser.add_argument(
        "--allowed-source-root",
        default=os.environ.get("KCP_ALLOWED_SOURCE_ROOT", str(Path.cwd())),
        help="only notebook sources beneath this directory may be staged",
    )
    parser.add_argument(
        "--adapter",
        choices=("fake", "kaggle"),
        default=os.environ.get("KCP_ADAPTER", "kaggle"),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("KCP_MAX_WORKERS", "10")),
    )
    parser.add_argument(
        "--max-jobs-per-account",
        type=int,
        default=int(os.environ.get("KCP_MAX_JOBS_PER_ACCOUNT", "2")),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("KCP_POLL_SECONDS", "5")),
    )
    parser.add_argument(
        "--live-log-poll-seconds",
        type=float,
        default=float(os.environ.get("KCP_LIVE_LOG_POLL_SECONDS", "30")),
    )
    parser.add_argument(
        "--quota-sync-seconds",
        type=float,
        default=float(os.environ.get("KCP_QUOTA_SYNC_SECONDS", "300")),
    )
    parser.add_argument(
        "--quota-start-delay-seconds",
        type=float,
        default=float(os.environ.get("KCP_QUOTA_START_DELAY_SECONDS", "0")),
    )
    parser.add_argument(
        "--max-source-bytes",
        type=int,
        default=int(os.environ.get("KCP_MAX_SOURCE_BYTES", str(100 * 1024 * 1024))),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    database_path = Path(args.db).expanduser().resolve()
    service = ControlPlaneService(
        database_path,
        data_dir=args.data_dir,
        allowed_source_root=args.allowed_source_root,
        adapter_name=args.adapter,
        max_workers=args.max_workers,
        max_jobs_per_account=args.max_jobs_per_account,
        remote_poll_seconds=args.poll_seconds,
        live_log_poll_seconds=args.live_log_poll_seconds,
        quota_sync_seconds=args.quota_sync_seconds,
        quota_start_delay_seconds=args.quota_start_delay_seconds,
        max_source_bytes=args.max_source_bytes,
    )
    server = create_server(
        service,
        host=args.host,
        port=args.port,
        api_token=os.environ.get("KCP_API_TOKEN"),
    )
    print(
        f"Kaggle Team Control Plane listening on http://{args.host}:{args.port} "
        f"(adapter={args.adapter}, db={database_path})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()

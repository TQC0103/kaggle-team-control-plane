"""Tiny workload used to verify the Kaggle Team Control Plane wiring."""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path


payload = {
    "ok": True,
    "message": "Kaggle Team Control Plane smoke test completed",
    "python": platform.python_version(),
    "finished_at": datetime.now(timezone.utc).isoformat(),
}

Path("/kaggle/working/result.json").write_text(
    json.dumps(payload, indent=2),
    encoding="utf-8",
)
print(json.dumps(payload))

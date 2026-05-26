from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


RUNTIME_DATA_DIR = Path(os.getenv("RUNTIME_DATA_DIR", "runtime-data"))
WORKER_HEARTBEAT_PATH = Path(
    os.getenv("DATA_CORE_WORKER_HEARTBEAT_PATH", str(RUNTIME_DATA_DIR / "worker_heartbeat.json"))
)


def write_worker_heartbeat(*, status: str, details: dict[str, Any] | None = None) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "status": status,
        "timestamp": now.isoformat(),
        "timestamp_epoch": now.timestamp(),
        "pid": os.getpid(),
        "details": details or {},
    }
    WORKER_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WORKER_HEARTBEAT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(WORKER_HEARTBEAT_PATH)


def read_worker_heartbeat() -> dict[str, Any] | None:
    try:
        if not WORKER_HEARTBEAT_PATH.exists():
            return None
        return json.loads(WORKER_HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


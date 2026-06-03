from __future__ import annotations

import json
from pathlib import Path

from app.runtime import heartbeat


def test_write_worker_heartbeat_writes_valid_json(tmp_path, monkeypatch):
    heartbeat_path = tmp_path / "worker_heartbeat.json"
    monkeypatch.setattr(heartbeat, "WORKER_HEARTBEAT_PATH", heartbeat_path)

    heartbeat.write_worker_heartbeat(status="idle", details={"phase": "test"})

    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert payload["status"] == "idle"
    assert payload["details"] == {"phase": "test"}
    assert payload["timestamp_epoch"] > 0


def test_write_worker_heartbeat_does_not_raise_when_replace_fails(tmp_path, monkeypatch):
    heartbeat_path = tmp_path / "worker_heartbeat.json"
    monkeypatch.setattr(heartbeat, "WORKER_HEARTBEAT_PATH", heartbeat_path)

    original_replace = Path.replace

    def fail_replace(self, target):
        if target == heartbeat_path:
            raise PermissionError("locked heartbeat")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    heartbeat.write_worker_heartbeat(status="running", details={"phase": "normalization"})

    assert not heartbeat_path.exists()

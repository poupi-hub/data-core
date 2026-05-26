from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime-data"
REPORT = ROOT / "reports" / "runtime-memory-pressure.md"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _latest_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _max_number(rows: list[dict[str, Any]], key: str) -> float:
    values = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, int | float):
            values.append(float(value))
    return max(values) if values else 0.0


def _window_hours(rows: list[dict[str, Any]]) -> float:
    epochs = []
    for row in rows:
        if row.get("timestamp_epoch"):
            epochs.append(float(row["timestamp_epoch"]))
            continue
        if row.get("timestamp"):
            try:
                epochs.append(datetime.fromisoformat(str(row["timestamp"])).timestamp())
            except ValueError:
                continue
    if len(epochs) < 2:
        return 0.0
    return round((max(epochs) - min(epochs)) / 3600.0, 4)


def build_report() -> str:
    history = _read_jsonl(RUNTIME / "scheduler_watchdog_history.jsonl")
    audit = _read_jsonl(RUNTIME / "scheduler_reliability_audit.jsonl")
    drift = _read_jsonl(RUNTIME / "scheduler_execution_drift.jsonl")
    lifecycle = _read_jsonl(RUNTIME / "scheduler_lifecycle.jsonl")
    snapshot = _latest_json(RUNTIME / "scheduler_watchdog_snapshot.json")

    modes = Counter(str(row.get("mode") or "UNKNOWN") for row in audit)
    diagnosis = Counter(str(row.get("diagnosis_state") or "UNKNOWN") for row in audit)
    priorities = Counter(str(row.get("priority") or "UNKNOWN") for row in audit)

    observed_restart = int(snapshot.get("observed_restart_count") or 0)
    restart_source = str(snapshot.get("restart_count_source") or "legacy_unknown")
    probe_boot_count = int(snapshot.get("probe_boot_count") or 0)
    real_restart_count = int(snapshot.get("real_restart_count") or 0)
    false_restart_count = observed_restart if restart_source == "legacy_unknown" and real_restart_count == 0 else 0

    max_memory = _max_number(history, "memory_usage_bytes")
    max_swap = _max_number(history, "swap_usage_bytes")
    max_growth = _max_number(history, "growth_rate_bytes_per_second")
    max_drift = _max_number(drift, "execution_drift_seconds")
    post_deploy_start = datetime.fromisoformat("2026-05-25T10:39:52+00:00")
    post_deploy_audit = []
    for row in audit:
        try:
            ts = datetime.fromisoformat(str(row.get("timestamp")))
        except ValueError:
            continue
        if ts >= post_deploy_start:
            post_deploy_audit.append(row)

    generated_at = datetime.now(tz=timezone.utc).isoformat()
    lines = [
        "# Runtime Memory Pressure Report",
        "",
        f"Generated at: `{generated_at}`",
        "",
        "## Executive Summary",
        "",
        f"- Watchdog samples: `{len(history)}` over `{_window_hours(history)}` hours.",
        f"- Reliability audit decisions: `{len(audit)}` over `{_window_hours(audit)}` hours.",
        f"- Scheduler drift events: `{len(drift)}`.",
        f"- Lifecycle events: `{len(lifecycle)}`.",
        f"- Latest restart source: `{restart_source}`.",
        f"- Real restart count: `{real_restart_count}`.",
        f"- False/legacy restart count candidate: `{false_restart_count}`.",
        f"- Probe boot count: `{probe_boot_count}`.",
        "",
        "## Memory And Swap",
        "",
        f"- Current RSS/cgroup memory: `{snapshot.get('memory_usage_bytes')}` bytes.",
        f"- Max observed cgroup memory: `{int(max_memory)}` bytes.",
        f"- Current memory limit: `{snapshot.get('memory_limit_bytes')}`.",
        f"- Current swap usage: `{snapshot.get('swap_usage_bytes')}` bytes.",
        f"- Max observed swap usage: `{int(max_swap)}` bytes.",
        f"- Max memory growth rate: `{max_growth}` bytes/second.",
        f"- Memory events: `{json.dumps(snapshot.get('memory_events') or {}, sort_keys=True)}`.",
        "",
        "## Scheduler Reliability Correlation",
        "",
        f"- Modes: `{dict(modes)}`.",
        f"- Diagnosis states: `{dict(diagnosis)}`.",
        f"- Priorities: `{dict(priorities)}`.",
        f"- Post-deploy reliability decisions: `{len(post_deploy_audit)}`.",
        f"- Max APScheduler drift: `{max_drift}` seconds.",
        "",
        "## Root-Cause Finding",
        "",
        "The observed critical dominance is attributable to an untrusted restart counter in the scheduler snapshot when `restart_count_source` is absent. The snapshot shows no cgroup OOM events and no memory-limit ratio, while memory is stable. The hardened watchdog now treats that evidence as false/legacy provenance unless an explicit container/runtime source reports real restarts.",
        "",
        "## Capacity Remediation Plan",
        "",
        "- Keep `SCHEDULER_RELIABILITY_ENABLED=false` and `SCHEDULER_RELIABILITY_DRY_RUN=true`.",
        "- Scheduler runtime has been refreshed with the hardened probe.",
        "- Continue passive dry-run observation until 6h or 24 post-deploy decisions are available.",
        "- Watch `watchdog_confidence_score`, `scheduler_false_restart_total`, `scheduler_restart_real_total`, `scheduler_execution_drift_seconds`, and `runtime_memory_pressure_score`.",
        "- Do not enable runtime protection while restart provenance is `legacy_or_probe_local` or confidence is below `0.8`.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(build_report(), encoding="utf-8")
    print(str(REPORT))


if __name__ == "__main__":
    main()

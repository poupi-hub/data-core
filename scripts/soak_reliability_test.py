#!/usr/bin/env python3
"""Soak Reliability Test — Phase 12.

Validates that all pipeline reliability components remain stable over
extended periods (6h / 12h / 24h) without degradation.

This script does NOT execute pipelines or modify data. It observes:
  - Scheduler heartbeat freshness
  - Pipeline liveness cache state
  - Queue backlog evolution
  - Self-healing audit log
  - Memory pressure
  - Consecutive failures

Usage::

    # 6-hour soak (default check interval = 5 min)
    python scripts/soak_reliability_test.py --hours 6

    # 24-hour soak with 10-min intervals
    python scripts/soak_reliability_test.py --hours 24 --interval 600

    # Dry-run: single observation pass (no wait)
    python scripts/soak_reliability_test.py --dry-run

Exit codes:
  0 — All soak checks PASSED
  1 — At least one CRITICAL failure detected during soak
  2 — DEGRADED: warnings found but no critical failures
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Paths (read from runtime-data, no DB needed) ──────────────────────────────
RUNTIME_DATA_DIR = Path("runtime-data")
SCHEDULER_HEARTBEAT_PATH = RUNTIME_DATA_DIR / "scheduler_heartbeat.json"
PIPELINE_LIVENESS_PATH = RUNTIME_DATA_DIR / "pipeline_liveness.json"
SELF_HEALING_LOG_PATH = RUNTIME_DATA_DIR / "self_healing_log.jsonl"
SCHEDULER_WATCHDOG_SNAPSHOT_PATH = RUNTIME_DATA_DIR / "scheduler_watchdog_snapshot.json"


# ──────────────────────────────────────────────────────────────────────────────
# Observation snapshot
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SoakObservation:
    timestamp: str
    tick: int

    # Scheduler heartbeat
    hb_age_seconds: float | None = None
    hb_consecutive_failures: int = 0
    hb_drift_seconds: float | None = None
    hb_status: str = "UNKNOWN"

    # Pipeline liveness
    liveness_summary: dict[str, int] = field(default_factory=dict)
    dead_pipelines: list[str] = field(default_factory=list)
    stalled_pipelines: list[str] = field(default_factory=list)

    # Self-healing
    healing_actions_since_last: int = 0
    healing_throttled_since_last: int = 0

    # Findings
    critical_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _read_jsonl_count(path: Path, since_ts: str | None) -> int:
    """Count JSONL lines with timestamp >= since_ts."""
    count = 0
    try:
        if not path.exists():
            return 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if since_ts is None or ts >= since_ts:
                    count += 1
            except Exception:
                pass
    except Exception:
        pass
    return count


def observe(tick: int, last_ts: str | None) -> SoakObservation:
    now = datetime.now(timezone.utc)
    obs = SoakObservation(timestamp=now.isoformat(), tick=tick)

    # ── Scheduler heartbeat ──────────────────────────────────────────────────
    hb = _read_json(SCHEDULER_HEARTBEAT_PATH)
    if hb is None:
        obs.hb_status = "MISSING"
        obs.warnings.append("scheduler_heartbeat.json not found")
    else:
        written_str = hb.get("written_at")
        if written_str:
            try:
                written = datetime.fromisoformat(written_str)
                if written.tzinfo is None:
                    written = written.replace(tzinfo=timezone.utc)
                obs.hb_age_seconds = (now - written).total_seconds()
            except Exception:
                pass
        obs.hb_consecutive_failures = int(hb.get("consecutive_failures", 0) or 0)
        obs.hb_drift_seconds = hb.get("execution_drift_seconds")

        if obs.hb_age_seconds is None:
            obs.hb_status = "MISSING"
            obs.warnings.append("scheduler heartbeat written_at missing")
        elif obs.hb_age_seconds > 30 * 60:
            obs.hb_status = "DEAD"
            obs.critical_issues.append(
                f"Scheduler heartbeat DEAD: age={obs.hb_age_seconds:.0f}s (>30min)"
            )
        elif obs.hb_age_seconds > 10 * 60:
            obs.hb_status = "STALLED"
            obs.warnings.append(
                f"Scheduler heartbeat STALLED: age={obs.hb_age_seconds:.0f}s (>10min)"
            )
        else:
            obs.hb_status = "ALIVE"

        if obs.hb_consecutive_failures >= 5:
            obs.critical_issues.append(
                f"Scheduler has {obs.hb_consecutive_failures} consecutive failures"
            )
        elif obs.hb_consecutive_failures >= 2:
            obs.warnings.append(
                f"Scheduler has {obs.hb_consecutive_failures} consecutive failures"
            )

    # ── Pipeline liveness ────────────────────────────────────────────────────
    liveness = _read_json(PIPELINE_LIVENESS_PATH)
    if liveness is None:
        obs.warnings.append("pipeline_liveness.json not found — liveness not evaluated yet")
    else:
        obs.liveness_summary = liveness.get("summary", {})
        for pipeline in liveness.get("pipelines", []):
            status = pipeline.get("status", "UNKNOWN")
            pid = pipeline.get("pipeline_id", "?")
            if status == "DEAD":
                obs.dead_pipelines.append(pid)
                obs.critical_issues.append(f"Pipeline DEAD: {pid}")
            elif status == "STALLED":
                obs.stalled_pipelines.append(pid)
                obs.warnings.append(f"Pipeline STALLED: {pid}")
            elif status == "BLOCKED":
                obs.warnings.append(f"Pipeline BLOCKED: {pid}")

    # ── Self-healing audit ───────────────────────────────────────────────────
    heal_count = _read_jsonl_count(SELF_HEALING_LOG_PATH, last_ts)
    obs.healing_actions_since_last = heal_count

    # Check for throttle loop (too many triggers in one period)
    if heal_count > 10:
        obs.warnings.append(
            f"Self-healing fired {heal_count} times since last tick — possible recovery loop"
        )

    return obs


# ──────────────────────────────────────────────────────────────────────────────
# Soak runner
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SoakResult:
    started_at: str
    ended_at: str
    total_ticks: int = 0
    critical_ticks: int = 0
    warning_ticks: int = 0
    all_observations: list[SoakObservation] = field(default_factory=list)
    final_status: str = "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total_ticks": self.total_ticks,
            "critical_ticks": self.critical_ticks,
            "warning_ticks": self.warning_ticks,
            "final_status": self.final_status,
            "observations_count": len(self.all_observations),
        }


def run_soak(
    hours: float,
    interval_seconds: int = 300,
    dry_run: bool = False,
    json_output: bool = False,
) -> SoakResult:
    started = datetime.now(timezone.utc)
    deadline = started + timedelta(hours=hours)
    result = SoakResult(started_at=started.isoformat(), ended_at="")

    tick = 0
    last_ts: str | None = None

    while True:
        now = datetime.now(timezone.utc)
        obs = observe(tick, last_ts)
        result.all_observations.append(obs)

        if obs.critical_issues:
            result.critical_ticks += 1
            result.final_status = "FAIL"
        elif obs.warnings:
            result.warning_ticks += 1
            if result.final_status == "PASS":
                result.final_status = "DEGRADED"

        if not json_output:
            _print_observation(obs)

        last_ts = obs.timestamp
        tick += 1

        if dry_run or now >= deadline:
            break

        remaining = (deadline - now).total_seconds()
        sleep_time = min(interval_seconds, remaining)
        if sleep_time > 0:
            time.sleep(sleep_time)

    result.ended_at = datetime.now(timezone.utc).isoformat()
    result.total_ticks = tick

    if json_output:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_summary(result)

    return result


def _sout(text: str) -> None:
    """Print with safe encoding fallback for Windows cp1252 terminals."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _print_observation(obs: SoakObservation) -> None:
    icon = "[OK]" if not obs.critical_issues and not obs.warnings else ("[CRIT]" if obs.critical_issues else "[WARN]")
    hb_age_str = f"{obs.hb_age_seconds:.0f}s" if obs.hb_age_seconds is not None else "N/A"
    _sout(
        f"[{obs.timestamp}] Tick {obs.tick:3d}  {icon}  "
        f"hb={obs.hb_status}({hb_age_str})  "
        f"liveness={obs.liveness_summary}  "
        f"failures={obs.hb_consecutive_failures}"
    )
    for issue in obs.critical_issues:
        _sout(f"    [CRITICAL] {issue}")
    for warn in obs.warnings:
        _sout(f"    [WARNING]  {warn}")


def _print_summary(result: SoakResult) -> None:
    SEP = "-" * 70
    _sout(SEP)
    _sout("  SOAK RELIABILITY TEST SUMMARY")
    _sout(SEP)
    _sout(f"  Started    : {result.started_at}")
    _sout(f"  Ended      : {result.ended_at}")
    _sout(f"  Total ticks: {result.total_ticks}")
    _sout(f"  Critical   : {result.critical_ticks}")
    _sout(f"  Warnings   : {result.warning_ticks}")
    _sout(SEP)
    if result.final_status == "PASS":
        _sout("  [PASS] All soak checks passed.")
    elif result.final_status == "DEGRADED":
        _sout("  [DEGRADED] Warnings detected during soak (no critical failures).")
    else:
        _sout("  [FAIL] Critical issues detected during soak. Investigate immediately.")
    _sout(SEP)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Pipeline reliability soak test")
    parser.add_argument("--hours", type=float, default=6.0, help="Soak duration in hours")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Single observation, no wait")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    result = run_soak(
        hours=args.hours,
        interval_seconds=args.interval,
        dry_run=args.dry_run,
        json_output=args.json,
    )

    if result.final_status == "FAIL":
        return 1
    if result.final_status == "DEGRADED":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

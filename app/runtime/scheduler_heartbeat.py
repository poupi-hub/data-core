"""Scheduler Heartbeat — Phase 2.

Real proof-of-execution heartbeat for the APScheduler process.

Unlike the container health-check (process alive), this heartbeat is written
INSIDE job callbacks — proving that jobs actually execute, not just that the
process is running.

Layout of runtime-data/scheduler_heartbeat.json:
  {
    "scheduler_started_at": "...",
    "last_job": "normalize_job",
    "last_job_at": "...",
    "last_job_status": "success",
    "last_job_duration_seconds": 1.23,
    "last_success_job": "normalize_job",
    "last_success_at": "...",
    "last_failure_job": null,
    "last_failure_at": null,
    "consecutive_failures": 0,
    "jobs_executed_total": 42,
    "execution_drift_seconds": 8.1,
    "missed_schedules": 0,
    "pid": 1234,
    "written_at": "..."
  }

Usage inside scheduler jobs::

    from app.runtime.scheduler_heartbeat import record_job_heartbeat

    @record_job_heartbeat("normalize_job")
    def normalize_job(module=None, limit=100):
        ...

Or call directly::

    record_job_execution("normalize_job", status="success", duration=1.23)
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.runtime.heartbeat import RUNTIME_DATA_DIR

logger = logging.getLogger(__name__)

SCHEDULER_HEARTBEAT_PATH = Path(
    os.getenv(
        "SCHEDULER_HEARTBEAT_PATH",
        str(RUNTIME_DATA_DIR / "scheduler_heartbeat.json"),
    )
)

# Tracks scheduler start time (set once on first write or scheduler boot)
_SCHEDULER_STARTED_AT: str | None = None
_JOBS_EXECUTED_TOTAL: int = 0
_CONSECUTIVE_FAILURES: int = 0
_MISSED_SCHEDULES: int = 0

# Expected intervals per job (seconds) for drift detection
_JOB_EXPECTED_INTERVALS: dict[str, int] = {
    "normalize_job": 15 * 60,
    "analytics_job": 60 * 60,
    "run_ecommerce_url_targets_job": 2 * 3600,
    "collect_raw_job": 60 * 60,
    "operational_watchdog_job": 30 * 60,
    "watchdog_heartbeat_job": 6 * 3600,
    "scheduler_heartbeat_job": 5 * 60,
    "cleanup_stale_runs_job": 15 * 60,
    "data_retention_job": 7 * 24 * 3600,
    "alert_webhook_job": 60 * 60,
}


def _read_current() -> dict[str, Any]:
    """Read current heartbeat state from file (or return empty baseline)."""
    try:
        if SCHEDULER_HEARTBEAT_PATH.exists():
            return json.loads(SCHEDULER_HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "scheduler_started_at": None,
        "last_job": None,
        "last_job_at": None,
        "last_job_status": None,
        "last_job_duration_seconds": None,
        "last_success_job": None,
        "last_success_at": None,
        "last_failure_job": None,
        "last_failure_at": None,
        "consecutive_failures": 0,
        "jobs_executed_total": 0,
        "execution_drift_seconds": None,
        "missed_schedules": 0,
        "pid": None,
        "written_at": None,
    }


def _write_atomic(payload: dict[str, Any]) -> None:
    """Write payload to SCHEDULER_HEARTBEAT_PATH atomically."""
    try:
        SCHEDULER_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SCHEDULER_HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(SCHEDULER_HEARTBEAT_PATH)
    except Exception as exc:
        logger.warning("scheduler_heartbeat: write failed: %s", exc)


def boot_heartbeat() -> None:
    """Call once when the scheduler process starts.

    Records ``scheduler_started_at`` so uptime can be computed.
    Safe to call multiple times — only sets start time if not already present.
    """
    global _SCHEDULER_STARTED_AT
    now_iso = datetime.now(timezone.utc).isoformat()
    current = _read_current()

    if not current.get("scheduler_started_at"):
        current["scheduler_started_at"] = now_iso
        _SCHEDULER_STARTED_AT = now_iso
    else:
        _SCHEDULER_STARTED_AT = current["scheduler_started_at"]

    current["pid"] = os.getpid()
    current["written_at"] = now_iso
    _write_atomic(current)
    logger.info("scheduler_heartbeat: boot recorded at %s", now_iso)


def record_job_execution(
    job_name: str,
    *,
    status: str,                           # "success" | "error" | "partial"
    duration_seconds: float | None = None,
    scheduled_at: float | None = None,    # monotonic time the job was supposed to start
) -> None:
    """Record execution of a single scheduler job.

    Should be called by every scheduler job after it finishes.
    Thread-safe: reads existing state, merges, writes atomically.

    Args:
        job_name: Canonical job name (e.g. "normalize_job").
        status: "success" | "error" | "partial".
        duration_seconds: Wall-clock run duration.
        scheduled_at: monotonic.time() when the job was supposed to fire, for
                      drift computation.  Pass ``time.monotonic()`` captured
                      at the top of the job wrapper.
    """
    global _JOBS_EXECUTED_TOTAL, _CONSECUTIVE_FAILURES

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    current = _read_current()

    # ── Drift detection ───────────────────────────────────────────────────────
    drift: float | None = None
    if scheduled_at is not None:
        actual_delay = time.monotonic() - scheduled_at
        expected = _JOB_EXPECTED_INTERVALS.get(job_name)
        if expected:
            # Drift = how many seconds late the job fired (vs expected interval)
            last_at_str = current.get("last_job_at")
            if last_at_str:
                try:
                    from datetime import datetime as _dt
                    last_at = _dt.fromisoformat(last_at_str)
                    if last_at.tzinfo is None:
                        last_at = last_at.replace(tzinfo=timezone.utc)
                    actual_interval = (now - last_at).total_seconds()
                    drift = max(0.0, actual_interval - expected)
                except Exception:
                    pass

    # ── Update counters ───────────────────────────────────────────────────────
    _JOBS_EXECUTED_TOTAL = current.get("jobs_executed_total", 0) + 1

    if status == "error":
        _CONSECUTIVE_FAILURES = current.get("consecutive_failures", 0) + 1
    else:
        _CONSECUTIVE_FAILURES = 0

    # ── Build updated payload ─────────────────────────────────────────────────
    payload: dict[str, Any] = {
        **current,
        "last_job": job_name,
        "last_job_at": now_iso,
        "last_job_status": status,
        "last_job_duration_seconds": duration_seconds,
        "consecutive_failures": _CONSECUTIVE_FAILURES,
        "jobs_executed_total": _JOBS_EXECUTED_TOTAL,
        "pid": os.getpid(),
        "written_at": now_iso,
    }

    if _SCHEDULER_STARTED_AT:
        payload["scheduler_started_at"] = _SCHEDULER_STARTED_AT

    if drift is not None:
        payload["execution_drift_seconds"] = round(drift, 1)

    if status in ("success", "partial"):
        payload["last_success_job"] = job_name
        payload["last_success_at"] = now_iso
    elif status == "error":
        payload["last_failure_job"] = job_name
        payload["last_failure_at"] = now_iso

    _write_atomic(payload)


def read_scheduler_heartbeat() -> dict[str, Any] | None:
    """Read the latest scheduler heartbeat without touching the DB.

    Returns None if the file does not exist or is corrupt.
    """
    try:
        if not SCHEDULER_HEARTBEAT_PATH.exists():
            return None
        return json.loads(SCHEDULER_HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def heartbeat_age_seconds() -> float | None:
    """Seconds since the last scheduler heartbeat was written.

    Returns None if no heartbeat exists.
    """
    hb = read_scheduler_heartbeat()
    if not hb:
        return None
    written_at_str = hb.get("written_at")
    if not written_at_str:
        return None
    try:
        from datetime import datetime as _dt
        written_at = _dt.fromisoformat(written_at_str)
        if written_at.tzinfo is None:
            written_at = written_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - written_at).total_seconds()
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Decorator helper
# ──────────────────────────────────────────────────────────────────────────────

def record_job_heartbeat(job_name: str) -> Callable:
    """Decorator that wraps a scheduler job with heartbeat recording.

    Usage::

        @record_job_heartbeat("normalize_job")
        def normalize_job(module=None, limit=100):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            started_monotonic = time.monotonic()
            status = "success"
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception:
                status = "error"
                raise
            finally:
                duration = time.monotonic() - started_monotonic
                try:
                    record_job_execution(
                        job_name,
                        status=status,
                        duration_seconds=round(duration, 3),
                        scheduled_at=started_monotonic,
                    )
                except Exception as exc:
                    logger.warning("scheduler_heartbeat: record failed: %s", exc)
        return wrapper
    return decorator

"""Preventive watchdog for the data-core scheduler container.

The scheduler process writes a small JSON snapshot with cgroup memory data to
the shared runtime-data volume. The API reads that snapshot and exposes a
read-only diagnosis endpoint and Prometheus gauges. This avoids mounting the
Docker socket into the API container.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.pipeline.models import PipelineRun
from app.raw.models import RawCollection

logger = logging.getLogger(__name__)

RUNTIME_DATA_DIR = Path(os.getenv("RUNTIME_DATA_DIR", "runtime-data"))
SNAPSHOT_PATH = Path(
    os.getenv(
        "DATA_CORE_SCHEDULER_WATCHDOG_SNAPSHOT_PATH",
        str(RUNTIME_DATA_DIR / "scheduler_watchdog_snapshot.json"),
    )
)
BOOT_COUNT_PATH = Path(
    os.getenv(
        "DATA_CORE_SCHEDULER_WATCHDOG_BOOT_COUNT_PATH",
        str(RUNTIME_DATA_DIR / "scheduler_watchdog_boot_count.txt"),
    )
)
HISTORY_PATH = Path(
    os.getenv(
        "DATA_CORE_SCHEDULER_WATCHDOG_HISTORY_PATH",
        str(RUNTIME_DATA_DIR / "scheduler_watchdog_history.jsonl"),
    )
)
LIFECYCLE_PATH = Path(
    os.getenv(
        "DATA_CORE_SCHEDULER_LIFECYCLE_PATH",
        str(RUNTIME_DATA_DIR / "scheduler_lifecycle.jsonl"),
    )
)
DRIFT_PATH = Path(
    os.getenv(
        "DATA_CORE_SCHEDULER_DRIFT_PATH",
        str(RUNTIME_DATA_DIR / "scheduler_execution_drift.jsonl"),
    )
)

STATE_VALUE = {
    "SCHEDULER_HEALTHY": 0,
    "SCHEDULER_MEMORY_ELEVATED": 1,
    "SCHEDULER_MEMORY_HIGH": 2,
    "SCHEDULER_MEMORY_CRITICAL": 3,
    "SCHEDULER_OOM_RECENT": 4,
    "SCHEDULER_RESTART_LOOP": 5,
    "SCHEDULER_DEGRADED": 6,
    "OBSERVE_MORE": 7,
}

SEVERITY_VALUE = {"info": 0, "warning": 1, "critical": 2}

TREND_MEMORY_STABLE = "MEMORY_STABLE"
TREND_MEMORY_GROWING = "MEMORY_GROWING"
TREND_MEMORY_SPIKING = "MEMORY_SPIKING"
TREND_POSSIBLE_MEMORY_LEAK = "POSSIBLE_MEMORY_LEAK"

SUMMARY_FIELDS = (
    "state",
    "severity",
    "memory_usage_ratio",
    "swap_usage_ratio",
    "restart_count",
    "oom_recent",
    "trend",
    "recommendation",
)


@dataclass
class SchedulerDiagnosis:
    container_name: str | None
    memory_usage_bytes: int | None
    memory_limit_bytes: int | None
    memory_usage_ratio: float | None
    swap_usage_ratio: float | None
    restart_count: int
    oom_recent: bool
    oom_total: int
    growth_rate: float
    trend_state: str
    operational_state: str
    alert_severity: str
    cycle_duration: float | None
    backlog_score: float
    explanation: str
    recommended_action: str
    snapshot_age_seconds: float | None = None
    snapshot_source: str = "runtime-data"
    restart_reason_chain: list[str] | None = None
    restart_provenance: str = "unknown"
    real_restart_count: int = 0
    false_restart_count: int = 0
    heartbeat_age_seconds: float | None = None
    watchdog_confidence_score: float = 0.0
    execution_drift_seconds: float = 0.0
    runtime_memory_pressure_score: float = 0.0
    swap_growth_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_summary(self) -> dict[str, Any]:
        return {
            "state": self.operational_state,
            "severity": self.alert_severity,
            "memory_usage_ratio": self.memory_usage_ratio,
            "memory_usage_percent": _round_percent(self.memory_usage_ratio),
            "swap_usage_ratio": self.swap_usage_ratio,
            "swap_usage_percent": _round_percent(self.swap_usage_ratio),
            "restart_count": self.restart_count,
            "oom_recent": self.oom_recent,
            "trend": self.trend_state,
            "recommendation": self.recommended_action,
            "restart_provenance": self.restart_provenance,
            "real_restart_count": self.real_restart_count,
            "false_restart_count": self.false_restart_count,
            "heartbeat_age_seconds": self.heartbeat_age_seconds,
            "watchdog_confidence_score": self.watchdog_confidence_score,
            "execution_drift_seconds": self.execution_drift_seconds,
            "runtime_memory_pressure_score": self.runtime_memory_pressure_score,
        }


class DataCoreSchedulerWatchdog:
    """Build scheduler diagnosis from a runtime snapshot plus DB backlog signals."""

    def __init__(
        self,
        snapshot_path: Path = SNAPSHOT_PATH,
        stale_after_seconds: int = 180,
    ) -> None:
        self.snapshot_path = snapshot_path
        self.stale_after_seconds = stale_after_seconds

    def diagnose(self, db: Session | None = None) -> SchedulerDiagnosis:
        snapshot = self._read_snapshot()
        cycle_duration = _latest_scheduler_cycle_duration(db)
        backlog_score = _scheduler_backlog_score(db)
        execution_drift = _latest_execution_drift()

        if snapshot is None:
            diagnosis = SchedulerDiagnosis(
                container_name=None,
                memory_usage_bytes=None,
                memory_limit_bytes=None,
                memory_usage_ratio=None,
                swap_usage_ratio=None,
                restart_count=0,
                oom_recent=False,
                oom_total=0,
                growth_rate=0.0,
                trend_state="OBSERVE_MORE",
                operational_state="SCHEDULER_DEGRADED",
                alert_severity="warning",
                cycle_duration=cycle_duration,
                backlog_score=backlog_score,
                explanation="Scheduler runtime snapshot is not available yet.",
                recommended_action="Confirm the scheduler process is running the watchdog probe and sharing runtime-data.",
                snapshot_age_seconds=None,
                restart_reason_chain=["snapshot_missing"],
                restart_provenance="missing_snapshot",
                watchdog_confidence_score=0.20,
                execution_drift_seconds=execution_drift,
            )
            update_scheduler_metrics(diagnosis)
            return diagnosis

        now = time.time()
        ts = float(snapshot.get("timestamp_epoch") or 0)
        snapshot_age = now - ts if ts > 0 else None
        stale = snapshot_age is None or snapshot_age > self.stale_after_seconds

        memory_usage = _as_int(snapshot.get("memory_usage_bytes"))
        memory_limit = _as_int(snapshot.get("memory_limit_bytes"))
        memory_ratio = _ratio(memory_usage, memory_limit)
        swap_ratio = _as_float(snapshot.get("swap_usage_ratio"))
        restart_evidence = _restart_evidence(snapshot)
        oom_total = max(0, _as_int(snapshot.get("oom_kill_count")) or 0)
        oom_recent = bool(snapshot.get("oom_recent"))
        growth_rate = _as_float(snapshot.get("growth_rate_bytes_per_second")) or 0.0
        trend_state = str(snapshot.get("trend_state") or TREND_MEMORY_STABLE)
        pressure_score = _runtime_memory_pressure_score(
            memory_ratio=memory_ratio,
            swap_ratio=swap_ratio,
            oom_recent=oom_recent,
            trend_state=trend_state,
        )
        swap_growth = _swap_growth_bytes(snapshot)
        heartbeat_age = snapshot_age
        confidence = _watchdog_confidence_score(
            stale=stale,
            restart_provenance=restart_evidence["provenance"],
            has_legacy_restart=restart_evidence["false_restart_count"] > 0,
            oom_recent=oom_recent,
            memory_ratio=memory_ratio,
            backlog_score=backlog_score,
        )

        operational_state, severity, explanation, action = self._classify(
            stale=stale,
            memory_ratio=memory_ratio,
            swap_ratio=swap_ratio,
            restart_count=restart_evidence["real_restart_count"],
            false_restart_count=restart_evidence["false_restart_count"],
            restart_provenance=restart_evidence["provenance"],
            oom_recent=oom_recent,
            trend_state=trend_state,
            backlog_score=backlog_score,
            cycle_duration=cycle_duration,
            execution_drift_seconds=execution_drift,
        )

        diagnosis = SchedulerDiagnosis(
            container_name=str(snapshot.get("container_name") or os.getenv("HOSTNAME") or "unknown"),
            memory_usage_bytes=memory_usage,
            memory_limit_bytes=memory_limit,
            memory_usage_ratio=memory_ratio,
            swap_usage_ratio=swap_ratio,
            restart_count=restart_evidence["real_restart_count"],
            oom_recent=oom_recent,
            oom_total=oom_total,
            growth_rate=growth_rate,
            trend_state=trend_state,
            operational_state=operational_state,
            alert_severity=severity,
            cycle_duration=cycle_duration,
            backlog_score=backlog_score,
            explanation=explanation,
            recommended_action=action,
            snapshot_age_seconds=snapshot_age,
            restart_reason_chain=restart_evidence["reason_chain"],
            restart_provenance=restart_evidence["provenance"],
            real_restart_count=restart_evidence["real_restart_count"],
            false_restart_count=restart_evidence["false_restart_count"],
            heartbeat_age_seconds=heartbeat_age,
            watchdog_confidence_score=confidence,
            execution_drift_seconds=execution_drift,
            runtime_memory_pressure_score=pressure_score,
            swap_growth_bytes=swap_growth,
        )
        update_scheduler_metrics(diagnosis)
        return diagnosis

    def _read_snapshot(self) -> dict[str, Any] | None:
        try:
            if not self.snapshot_path.exists():
                return None
            return json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read scheduler watchdog snapshot")
            return None

    def _classify(
        self,
        *,
        stale: bool,
        memory_ratio: float | None,
        swap_ratio: float | None,
        restart_count: int,
        false_restart_count: int,
        restart_provenance: str,
        oom_recent: bool,
        trend_state: str,
        backlog_score: float,
        cycle_duration: float | None,
        execution_drift_seconds: float,
    ) -> tuple[str, str, str, str]:
        if stale:
            return (
                "SCHEDULER_DEGRADED",
                "warning",
                "Scheduler telemetry snapshot is stale.",
                "Validate scheduler container health and runtime-data mount.",
            )
        if false_restart_count >= 3 and restart_count == 0:
            return (
                "OBSERVE_MORE",
                "warning",
                "Legacy or probe-local boot counter was reported as restart count; no real container restart provenance is present.",
                "Treat restart-loop signal as untrusted until lifecycle provenance is refreshed by the current scheduler probe.",
            )
        if restart_count >= 3:
            return (
                "SCHEDULER_RESTART_LOOP",
                "critical",
                f"Scheduler process has restarted repeatedly according to {restart_provenance} provenance.",
                "Inspect recent container logs, OOM events, and host memory pressure before changing concurrency.",
            )
        if oom_recent:
            return (
                "SCHEDULER_OOM_RECENT",
                "critical",
                "Recent scheduler cgroup OOM kill was detected.",
                "Keep current memory mitigation, inspect memory growth, and prepare host scale-up if repeated.",
            )
        if memory_ratio is not None and memory_ratio >= 0.90:
            return (
                "SCHEDULER_MEMORY_CRITICAL",
                "critical",
                "Scheduler memory usage is above 90% of its container limit.",
                "Watch the next cycles closely and consider raising memory or scaling the host if pressure persists.",
            )
        if memory_ratio is not None and memory_ratio >= 0.75:
            return (
                "SCHEDULER_MEMORY_HIGH",
                "warning",
                "Scheduler memory usage is above 75% of its container limit.",
                "Observe the next cycles and confirm the growth trend is not persistent.",
            )
        if swap_ratio is not None and swap_ratio >= 0.70:
            return (
                "SCHEDULER_MEMORY_HIGH",
                "warning",
                "Scheduler swap usage is elevated.",
                "Correlate with host swap pressure and scheduler cycle duration.",
            )
        if trend_state == TREND_POSSIBLE_MEMORY_LEAK:
            return (
                "OBSERVE_MORE",
                "warning",
                "Scheduler memory has grown steadily across recent samples.",
                "Collect more samples and inspect allocations if the trend continues.",
            )
        if backlog_score >= 0.75 or (cycle_duration is not None and cycle_duration >= 900):
            return (
                "SCHEDULER_DEGRADED",
                "warning",
                "Scheduler runtime signals suggest slow cycles or backlog pressure.",
                "Inspect collection and normalization runs before changing concurrency.",
            )
        if execution_drift_seconds >= 300:
            return (
                "SCHEDULER_DEGRADED",
                "warning",
                "APScheduler execution drift is elevated.",
                "Correlate job delay with CPU, memory pressure, and scheduler overlap before changing intervals.",
            )
        if memory_ratio is not None and memory_ratio >= 0.60:
            return (
                "SCHEDULER_MEMORY_ELEVATED",
                "info",
                "Scheduler memory is elevated but below warning thresholds.",
                "Continue observation; no operational change is required.",
            )
        return (
            "SCHEDULER_HEALTHY",
            "info",
            "Scheduler memory and runtime signals are within preventive thresholds.",
            "No action required.",
        )


def start_scheduler_watchdog_probe(interval_seconds: int = 30) -> threading.Event:
    """Start a daemon probe in the scheduler process."""
    stop_event = threading.Event()
    boot_count = _increment_boot_count()
    process_started_at = datetime.now(timezone.utc)
    process_started_epoch = time.time()
    process_id = os.getpid()
    previous_oom_kill = _load_previous_oom_kill()
    append_scheduler_lifecycle_event(
        "probe_started",
        {
            "probe_boot_count": boot_count,
            "process_id": process_id,
            "process_started_at": process_started_at.isoformat(),
            "restart_count_source": "probe_only",
        },
    )

    def _loop() -> None:
        nonlocal previous_oom_kill
        samples: list[dict[str, Any]] = _load_history(limit=20)
        heartbeat_sequence = 0
        while not stop_event.is_set():
            try:
                heartbeat_sequence += 1
                sample = _collect_scheduler_sample(
                    boot_count=boot_count,
                    heartbeat_sequence=heartbeat_sequence,
                    process_id=process_id,
                    process_started_at=process_started_at,
                    process_started_epoch=process_started_epoch,
                    previous_oom_kill=previous_oom_kill,
                    samples=samples,
                )
                previous_oom_kill = int(sample.get("oom_kill_count") or previous_oom_kill)
                samples.append(sample)
                samples = samples[-20:]
                _write_snapshot(sample)
                _append_history(sample)
            except Exception:
                logger.exception("Scheduler watchdog probe failed")
            stop_event.wait(interval_seconds)

    thread = threading.Thread(target=_loop, name="scheduler-watchdog-probe", daemon=True)
    thread.start()
    return stop_event


def _collect_scheduler_sample(
    *,
    boot_count: int,
    heartbeat_sequence: int,
    process_id: int,
    process_started_at: datetime,
    process_started_epoch: float,
    previous_oom_kill: int,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    now = time.time()
    memory_usage = _read_cgroup_int("memory.current")
    memory_limit = _read_cgroup_limit("memory.max")
    swap_usage = _read_cgroup_int("memory.swap.current")
    swap_limit = _read_cgroup_limit("memory.swap.max")
    events = _read_cgroup_events()
    oom_kill = int(events.get("oom_kill", 0))
    oom_recent = oom_kill > previous_oom_kill
    growth_rate = _growth_rate(samples + [{"timestamp_epoch": now, "memory_usage_bytes": memory_usage}])
    trend_state = _trend_state(samples + [{"timestamp_epoch": now, "memory_usage_bytes": memory_usage}], memory_limit)

    return {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "timestamp_epoch": now,
        "container_name": os.getenv("HOSTNAME", "data-core-scheduler"),
        "memory_usage_bytes": memory_usage,
        "memory_limit_bytes": memory_limit,
        "memory_usage_ratio": _ratio(memory_usage, memory_limit),
        "swap_usage_bytes": swap_usage,
        "swap_limit_bytes": swap_limit,
        "swap_usage_ratio": _ratio(swap_usage, swap_limit),
        # This probe runs inside the container and cannot observe Docker restart
        # count directly. Keep boot_count as evidence, but do not classify it as
        # a real restart loop; Docker/system-status must supply that truth.
        "observed_restart_count": 0,
        "probe_boot_count": boot_count,
        "heartbeat_sequence": heartbeat_sequence,
        "process_id": process_id,
        "process_started_at": process_started_at.isoformat(),
        "process_uptime_seconds": max(0, int(now - process_started_epoch)),
        "restart_count_source": "probe_only",
        "restart_reason_chain": ["probe_heartbeat"],
        "oom_kill_count": oom_kill,
        "oom_recent": oom_recent,
        "memory_events": events,
        "growth_rate_bytes_per_second": growth_rate,
        "trend_state": trend_state,
        "source": "cgroup",
    }


def update_scheduler_metrics(diagnosis: SchedulerDiagnosis) -> None:
    try:
        from api.metrics import (
            data_core_scheduler_backlog_score,
            data_core_scheduler_alert_severity,
            data_core_scheduler_cycle_duration_seconds,
            data_core_scheduler_growth_rate,
            data_core_scheduler_memory_limit_bytes,
            data_core_scheduler_memory_usage_bytes,
            data_core_scheduler_memory_usage_ratio,
            data_core_scheduler_oom_events_total,
            data_core_scheduler_restart_count,
            data_core_scheduler_state,
            data_core_scheduler_swap_usage_ratio,
            runtime_memory_pressure_score,
            runtime_swap_growth_bytes,
            scheduler_execution_drift_seconds,
            scheduler_false_restart_total,
            scheduler_heartbeat_age_seconds,
            scheduler_restart_loop_total,
            scheduler_restart_real_total,
            watchdog_confidence_score,
        )

        if diagnosis.memory_usage_bytes is not None:
            data_core_scheduler_memory_usage_bytes.set(diagnosis.memory_usage_bytes)
        if diagnosis.memory_limit_bytes is not None:
            data_core_scheduler_memory_limit_bytes.set(diagnosis.memory_limit_bytes)
        if diagnosis.memory_usage_ratio is not None:
            data_core_scheduler_memory_usage_ratio.set(diagnosis.memory_usage_ratio)
        if diagnosis.swap_usage_ratio is not None:
            data_core_scheduler_swap_usage_ratio.set(diagnosis.swap_usage_ratio)
        data_core_scheduler_restart_count.set(diagnosis.restart_count)
        data_core_scheduler_oom_events_total.set(diagnosis.oom_total)
        data_core_scheduler_state.set(STATE_VALUE.get(diagnosis.operational_state, 7))
        data_core_scheduler_alert_severity.set(SEVERITY_VALUE.get(diagnosis.alert_severity, 1))
        data_core_scheduler_growth_rate.set(diagnosis.growth_rate)
        if diagnosis.cycle_duration is not None:
            data_core_scheduler_cycle_duration_seconds.set(diagnosis.cycle_duration)
        data_core_scheduler_backlog_score.set(diagnosis.backlog_score)
        scheduler_restart_loop_total.set(1 if diagnosis.operational_state == "SCHEDULER_RESTART_LOOP" else 0)
        scheduler_restart_real_total.set(diagnosis.real_restart_count)
        scheduler_false_restart_total.set(diagnosis.false_restart_count)
        if diagnosis.heartbeat_age_seconds is not None:
            scheduler_heartbeat_age_seconds.set(diagnosis.heartbeat_age_seconds)
        scheduler_execution_drift_seconds.set(diagnosis.execution_drift_seconds)
        runtime_memory_pressure_score.set(diagnosis.runtime_memory_pressure_score)
        runtime_swap_growth_bytes.set(diagnosis.swap_growth_bytes)
        watchdog_confidence_score.set(diagnosis.watchdog_confidence_score)
    except Exception:
        logger.exception("Failed to update scheduler watchdog metrics")


def format_scheduler_telegram_message(diagnosis: SchedulerDiagnosis) -> str:
    pct = _fmt_pct(diagnosis.memory_usage_ratio)
    swap = _fmt_pct(diagnosis.swap_usage_ratio)
    if diagnosis.alert_severity == "critical":
        title = "Scheduler proximo de OOM"
    elif diagnosis.alert_severity == "warning":
        title = "Scheduler com pressao moderada"
    else:
        title = "Scheduler saudavel"
    return "\n".join(
        [
            f"<b>{title}</b>",
            f"Uso memoria: {pct}",
            f"Swap: {swap}",
            f"Restart loop: {'sim' if diagnosis.operational_state == 'SCHEDULER_RESTART_LOOP' else 'nao'}",
            f"OOM recente: {'sim' if diagnosis.oom_recent else 'nao'}",
            f"Estado: {diagnosis.operational_state}",
            f"Tendencia: {diagnosis.trend_state}",
            f"Acao: {diagnosis.recommended_action}",
        ]
    )


def format_scheduler_alert_payload(
    diagnosis: SchedulerDiagnosis,
    event: str | None = None,
) -> dict[str, Any]:
    """Build the operational Telegram alert payload without sending it."""
    alert_event = event or _event_for_diagnosis(diagnosis)
    emoji = {
        "healthy": "🟢",
        "warning": "⚠️",
        "critical": "🚨",
        "recovered": "✅",
        "degraded": "🟠",
    }.get(alert_event, "ℹ️")

    if alert_event == "recovered":
        title = "Scheduler recuperado"
    elif alert_event == "critical":
        title = "Scheduler em risco critico"
    elif alert_event == "warning":
        title = "Scheduler com pressao preventiva"
    elif alert_event == "degraded":
        title = "Scheduler degradado"
    else:
        title = "Scheduler saudavel"

    lines = [
        f"{emoji} <b>{title}</b>",
        f"Estado: {diagnosis.operational_state}",
        f"Severidade: {diagnosis.alert_severity}",
        f"Memoria: {_fmt_pct(diagnosis.memory_usage_ratio)}",
        f"Swap: {_fmt_pct(diagnosis.swap_usage_ratio)}",
        f"Restarts: {diagnosis.restart_count}",
        f"OOM recente: {'sim' if diagnosis.oom_recent else 'nao'}",
        f"Tendencia: {diagnosis.trend_state}",
        f"Backlog: {diagnosis.backlog_score:.2f}",
        f"Acao: {diagnosis.recommended_action}",
    ]

    return {
        "event": alert_event,
        "severity": diagnosis.alert_severity,
        "state": diagnosis.operational_state,
        "text": "\n".join(lines),
        "summary": diagnosis.to_summary(),
    }


def _event_for_diagnosis(diagnosis: SchedulerDiagnosis) -> str:
    if diagnosis.operational_state == "SCHEDULER_HEALTHY":
        return "healthy"
    if diagnosis.operational_state in {"SCHEDULER_DEGRADED", "OBSERVE_MORE"}:
        return "degraded"
    if diagnosis.alert_severity == "critical":
        return "critical"
    if diagnosis.alert_severity == "warning":
        return "warning"
    return "healthy"


def _latest_scheduler_cycle_duration(db: Session | None) -> float | None:
    if db is None:
        return None
    try:
        run = (
            db.query(PipelineRun)
            .filter(PipelineRun.trigger == "scheduler", PipelineRun.duration_seconds.is_not(None))
            .order_by(PipelineRun.finished_at.desc().nullslast(), PipelineRun.started_at.desc())
            .first()
        )
        if run and run.duration_seconds is not None:
            return float(run.duration_seconds)
    except Exception:
        logger.debug("PipelineRun duration lookup failed", exc_info=True)
    return None


def _scheduler_backlog_score(db: Session | None) -> float:
    if db is None:
        return 0.0
    try:
        pending = (
            db.query(func.count(RawCollection.id))
            .filter(RawCollection.processing_status == "normalization_pending")
            .scalar()
        )
        if not pending:
            return 0.0
        return min(1.0, float(pending) / 1000.0)
    except Exception:
        logger.debug("Backlog lookup failed", exc_info=True)
        return 0.0


def append_scheduler_lifecycle_event(event: str, details: dict[str, Any] | None = None) -> None:
    payload = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "timestamp_epoch": time.time(),
        "event": event,
        "details": details or {},
    }
    try:
        LIFECYCLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LIFECYCLE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        logger.exception("Failed to append scheduler lifecycle event")


def record_scheduler_execution_event(
    *,
    event: str,
    job_id: str | None,
    scheduled_run_time: datetime | None,
    exception: str | None = None,
) -> float:
    now = datetime.now(tz=timezone.utc)
    drift = 0.0
    if scheduled_run_time is not None:
        scheduled = scheduled_run_time
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        drift = max(0.0, (now - scheduled.astimezone(timezone.utc)).total_seconds())
    payload = {
        "timestamp": now.isoformat(),
        "timestamp_epoch": now.timestamp(),
        "event": event,
        "job_id": job_id,
        "scheduled_run_time": scheduled_run_time.isoformat() if scheduled_run_time else None,
        "execution_drift_seconds": drift,
        "exception": exception,
    }
    try:
        DRIFT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DRIFT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        logger.exception("Failed to append scheduler drift event")
    return drift


def _latest_execution_drift() -> float:
    try:
        if not DRIFT_PATH.exists():
            return 0.0
        for line in reversed(DRIFT_PATH.read_text(encoding="utf-8").splitlines()[-100:]):
            if not line.strip():
                continue
            payload = json.loads(line)
            return _as_float(payload.get("execution_drift_seconds")) or 0.0
    except Exception:
        logger.debug("Scheduler drift lookup failed", exc_info=True)
    return 0.0


def _restart_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    source = str(snapshot.get("restart_count_source") or "legacy_unknown")
    explicit_real = max(0, _as_int(snapshot.get("real_restart_count")) or 0)
    observed = max(0, _as_int(snapshot.get("observed_restart_count")) or 0)
    probe_boot_count = max(0, _as_int(snapshot.get("probe_boot_count")) or 0)
    reason_chain = list(snapshot.get("restart_reason_chain") or [])

    if source in {"docker", "container_runtime", "supervisor"}:
        real_restart_count = max(explicit_real, observed)
        false_restart_count = 0
        reason_chain.append(f"real_restart_source:{source}")
    elif explicit_real > 0:
        real_restart_count = explicit_real
        false_restart_count = 0
        reason_chain.append("real_restart_count_explicit")
    else:
        real_restart_count = 0
        false_restart_count = max(observed, 0)
        if false_restart_count:
            reason_chain.append("legacy_observed_restart_count_untrusted")
        if probe_boot_count:
            reason_chain.append("probe_boot_count_not_restart_count")
        source = "legacy_or_probe_local"

    if not reason_chain:
        reason_chain.append("no_restart_evidence")

    return {
        "provenance": source,
        "real_restart_count": real_restart_count,
        "false_restart_count": false_restart_count,
        "reason_chain": reason_chain,
    }


def _runtime_memory_pressure_score(
    *,
    memory_ratio: float | None,
    swap_ratio: float | None,
    oom_recent: bool,
    trend_state: str,
) -> float:
    score = 0.0
    if memory_ratio is not None:
        score = max(score, min(1.0, memory_ratio))
    if swap_ratio is not None:
        score = max(score, min(1.0, swap_ratio * 1.25))
    if trend_state == TREND_POSSIBLE_MEMORY_LEAK:
        score = max(score, 0.65)
    elif trend_state == TREND_MEMORY_SPIKING:
        score = max(score, 0.55)
    elif trend_state == TREND_MEMORY_GROWING:
        score = max(score, 0.40)
    if oom_recent:
        score = 1.0
    return round(score, 4)


def _swap_growth_bytes(snapshot: dict[str, Any]) -> int:
    current = _as_int(snapshot.get("swap_usage_bytes")) or 0
    previous = 0
    try:
        history = _load_history(limit=2)
        if history:
            previous = _as_int(history[-1].get("swap_usage_bytes")) or 0
    except Exception:
        previous = 0
    return max(0, current - previous)


def _watchdog_confidence_score(
    *,
    stale: bool,
    restart_provenance: str,
    has_legacy_restart: bool,
    oom_recent: bool,
    memory_ratio: float | None,
    backlog_score: float,
) -> float:
    if stale:
        return 0.30
    score = 0.95
    if restart_provenance == "legacy_or_probe_local" and has_legacy_restart:
        score = 0.35
    if memory_ratio is None:
        score = min(score, 0.80)
    if oom_recent or (memory_ratio is not None and memory_ratio >= 0.90) or backlog_score >= 0.90:
        score = max(score, 0.90)
    return round(score, 4)


def _increment_boot_count() -> int:
    try:
        BOOT_COUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
        current = 0
        if BOOT_COUNT_PATH.exists():
            current = int(BOOT_COUNT_PATH.read_text(encoding="utf-8").strip() or "0")
        current += 1
        BOOT_COUNT_PATH.write_text(str(current), encoding="utf-8")
        return current
    except Exception:
        logger.exception("Failed to update scheduler watchdog boot count")
        return 1


def _load_previous_oom_kill() -> int:
    try:
        if not SNAPSHOT_PATH.exists():
            return 0
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return int(data.get("oom_kill_count") or 0)
    except Exception:
        return 0


def _read_cgroup_int(filename: str) -> int | None:
    try:
        raw = Path("/sys/fs/cgroup", filename).read_text(encoding="utf-8").strip()
        if raw == "max":
            return None
        return int(raw)
    except Exception:
        return None


def _read_cgroup_limit(filename: str) -> int | None:
    value = _read_cgroup_int(filename)
    return value if value and value > 0 else None


def _read_cgroup_events() -> dict[str, int]:
    try:
        events: dict[str, int] = {}
        for line in Path("/sys/fs/cgroup/memory.events").read_text(encoding="utf-8").splitlines():
            key, value = line.split(maxsplit=1)
            events[key] = int(value)
        return events
    except Exception:
        return {}


def _write_snapshot(sample: dict[str, Any]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sample, sort_keys=True), encoding="utf-8")
    tmp.replace(SNAPSHOT_PATH)


def _append_history(sample: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, sort_keys=True) + "\n")


def _load_history(limit: int = 20) -> list[dict[str, Any]]:
    try:
        if not HISTORY_PATH.exists():
            return []
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
    except Exception:
        return []


def _growth_rate(samples: list[dict[str, Any]]) -> float:
    usable = [
        (float(s.get("timestamp_epoch") or 0), _as_int(s.get("memory_usage_bytes")))
        for s in samples
        if s.get("timestamp_epoch") and s.get("memory_usage_bytes") is not None
    ]
    if len(usable) < 2:
        return 0.0
    first_t, first_mem = usable[0]
    last_t, last_mem = usable[-1]
    if first_mem is None or last_mem is None or last_t <= first_t:
        return 0.0
    return (last_mem - first_mem) / (last_t - first_t)


def _trend_state(samples: list[dict[str, Any]], memory_limit: int | None) -> str:
    usable = [
        (float(s.get("timestamp_epoch") or 0), _as_int(s.get("memory_usage_bytes")))
        for s in samples
        if s.get("timestamp_epoch") and s.get("memory_usage_bytes") is not None
    ]
    if len(usable) < 3:
        return TREND_MEMORY_STABLE
    values = [m for _, m in usable if m is not None]
    if len(values) < 3:
        return TREND_MEMORY_STABLE
    delta = values[-1] - values[0]
    max_delta = max(values) - min(values)
    limit = memory_limit or max(values) or 1
    if max_delta / limit >= 0.15 and values[-1] < max(values):
        return TREND_MEMORY_SPIKING
    increases = sum(1 for prev, curr in zip(values, values[1:]) if curr > prev)
    if increases >= len(values) - 1 and delta / limit >= 0.10:
        return TREND_POSSIBLE_MEMORY_LEAK
    if delta / limit >= 0.05:
        return TREND_MEMORY_GROWING
    return TREND_MEMORY_STABLE


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return max(0.0, numerator / denominator)


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "desconhecido"
    return f"{value * 100:.0f}%"


def _round_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 100, 2)

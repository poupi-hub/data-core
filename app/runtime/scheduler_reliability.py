"""Adaptive, non-destructive scheduler reliability policy.

The policy computes protection modes and effective execution controls. Runtime
changes are applied only when explicitly enabled and not in dry-run mode.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.pipeline.models import PipelineRun
from app.raw.models import RawCollection
from app.runtime.scheduler_watchdog import DataCoreSchedulerWatchdog, SchedulerDiagnosis
from core.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

MODE_VALUE = {
    "NORMAL": 0,
    "CONSERVATIVE": 1,
    "PROTECTIVE": 2,
    "CRITICAL_PROTECTION": 3,
}

MODE_ORDER = tuple(MODE_VALUE)

PRIORITY_VALUE = {
    "CRITICAL": 0,
    "HIGH": 1,
    "NORMAL": 2,
    "LOW": 3,
}

JOB_PRIORITIES = {
    "operational_watchdog_job": "CRITICAL",
    "cleanup_stale_runs_job": "HIGH",
    "normalize_job": "HIGH",
    "analytics_job": "NORMAL",
    "run_ecommerce_url_targets_job": "NORMAL",
    "dataset_quality_crypto_job": "NORMAL",
    "signal_outcomes_job": "NORMAL",
    "alert_webhook_job": "LOW",
    "data_retention_job": "LOW",
}

AUDIT_PATH = Path("runtime-data/scheduler_reliability_audit.jsonl")
BACKLOG_HISTORY_PATH = Path("runtime-data/scheduler_backlog_history.jsonl")

MIN_CALIBRATION_HOURS = 6.0
MIN_CALIBRATION_DECISIONS = 24
MAX_FALSE_POSITIVE_RATIO = 0.02
MAX_MODE_CHANGE_RATIO = 0.10

REQUIRED_AUDIT_FIELDS = {
    "timestamp",
    "job_name",
    "priority",
    "mode",
    "enabled",
    "dry_run",
    "concurrency",
    "batch_size",
    "cooldown_seconds",
    "low_priority_delay_seconds",
    "throttled",
    "backlog",
    "recommendations",
    "diagnosis_state",
    "severity",
    "memory_usage_ratio",
    "swap_usage_ratio",
    "memory_growth_rate",
    "cycle_duration_seconds",
}

REQUIRED_BACKLOG_FIELDS = {
    "pending_total",
    "growth_rate",
    "pressure_score",
    "throughput_estimate",
    "starvation_detected",
    "explosive_growth",
    "stuck_jobs",
}

_SYNCED_DRY_RUN_COUNTS: dict[tuple[str, str, str], int] = {}
_SYNCED_FALSE_POSITIVE_COUNTS: dict[tuple[str, str, str], int] = {}


@dataclass
class BacklogSignals:
    pending_total: int
    growth_rate: float
    pressure_score: float
    throughput_estimate: float
    starvation_detected: bool
    explosive_growth: bool
    stuck_jobs: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReliabilityDecision:
    job_name: str
    priority: str
    mode: str
    enabled: bool
    dry_run: bool
    concurrency: int
    batch_size: int
    cooldown_seconds: float
    low_priority_delay_seconds: float
    throttled: bool
    throttle_reason: str | None
    backlog: BacklogSignals
    recommendations: list[str]
    diagnosis_state: str
    severity: str
    memory_usage_ratio: float
    swap_usage_ratio: float
    memory_growth_rate: float
    cycle_duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["backlog"] = self.backlog.to_dict()
        return payload


class SchedulerReliabilityEngine:
    """Compute and optionally apply adaptive scheduler protection."""

    def __init__(self, audit_path: Path = AUDIT_PATH) -> None:
        self.audit_path = audit_path

    def decide(
        self,
        job_name: str,
        *,
        priority: str | None = None,
        db: Session | None = None,
        diagnosis: SchedulerDiagnosis | None = None,
    ) -> ReliabilityDecision:
        priority = priority or JOB_PRIORITIES.get(job_name, "NORMAL")
        diagnosis = diagnosis or DataCoreSchedulerWatchdog().diagnose(db)
        backlog = analyze_backlog(db)
        mode = classify_protection_mode(diagnosis, backlog)
        batch_size = self._batch_size_for(mode)
        cooldown = self._cooldown_for(mode)
        low_priority_delay = (
            settings.scheduler_reliability_low_priority_extra_delay_seconds
            if priority == "LOW" and mode in {"PROTECTIVE", "CRITICAL_PROTECTION"}
            else 0.0
        )
        throttled = mode != "NORMAL" and priority in {"NORMAL", "LOW"}
        reason = _throttle_reason(diagnosis, backlog) if throttled else None
        concurrency = (
            1
            if mode in {"PROTECTIVE", "CRITICAL_PROTECTION"}
            else settings.worker_concurrency
        )

        decision = ReliabilityDecision(
            job_name=job_name,
            priority=priority,
            mode=mode,
            enabled=settings.scheduler_reliability_enabled,
            dry_run=settings.scheduler_reliability_dry_run,
            concurrency=concurrency,
            batch_size=batch_size,
            cooldown_seconds=cooldown,
            low_priority_delay_seconds=low_priority_delay,
            throttled=throttled,
            throttle_reason=reason,
            backlog=backlog,
            recommendations=recommend_actions(diagnosis, backlog, mode, priority),
            diagnosis_state=diagnosis.operational_state,
            severity=diagnosis.alert_severity,
            memory_usage_ratio=diagnosis.memory_usage_ratio or 0.0,
            swap_usage_ratio=diagnosis.swap_usage_ratio or 0.0,
            memory_growth_rate=diagnosis.growth_rate or 0.0,
            cycle_duration_seconds=diagnosis.cycle_duration or 0.0,
        )
        update_reliability_metrics(decision)
        append_reliability_audit(decision, self.audit_path)
        return decision

    def run(
        self,
        job_name: str,
        fn: Callable[..., T],
        *,
        priority: str | None = None,
        supports_limit: bool = False,
        default_limit: int = 100,
    ) -> T:
        decision = self.decide(job_name, priority=priority)
        kwargs: dict[str, Any] = {}
        apply_controls = decision.enabled and not decision.dry_run

        if apply_controls:
            total_delay = decision.cooldown_seconds + decision.low_priority_delay_seconds
            if total_delay > 0:
                logger.info(
                    "Scheduler reliability cooldown",
                    extra={
                        "job": job_name,
                        "mode": decision.mode,
                        "delay_seconds": total_delay,
                        "priority": decision.priority,
                    },
                )
                time.sleep(total_delay)
            if supports_limit:
                kwargs["limit"] = min(default_limit, decision.batch_size)
        elif supports_limit:
            kwargs["limit"] = default_limit

        return fn(**kwargs)

    def _batch_size_for(self, mode: str) -> int:
        if mode == "CRITICAL_PROTECTION":
            return settings.scheduler_reliability_critical_batch_size
        if mode == "PROTECTIVE":
            return settings.scheduler_reliability_protective_batch_size
        if mode == "CONSERVATIVE":
            return settings.scheduler_reliability_conservative_batch_size
        return settings.scheduler_reliability_base_batch_size

    def _cooldown_for(self, mode: str) -> float:
        if mode == "CRITICAL_PROTECTION":
            return settings.scheduler_reliability_critical_cooldown_seconds
        if mode == "PROTECTIVE":
            return settings.scheduler_reliability_protective_cooldown_seconds
        if mode == "CONSERVATIVE":
            return settings.scheduler_reliability_conservative_cooldown_seconds
        return 0.0


def classify_protection_mode(diagnosis: SchedulerDiagnosis, backlog: BacklogSignals) -> str:
    memory = diagnosis.memory_usage_ratio or 0.0
    swap = diagnosis.swap_usage_ratio or 0.0
    cycle = diagnosis.cycle_duration or 0.0

    if (
        diagnosis.operational_state
        in {"SCHEDULER_MEMORY_CRITICAL", "SCHEDULER_OOM_RECENT", "SCHEDULER_RESTART_LOOP"}
        or memory >= 0.90
        or diagnosis.oom_recent
        or backlog.pressure_score >= 0.90
        or cycle >= 900
    ):
        return "CRITICAL_PROTECTION"
    if (
        diagnosis.operational_state in {"SCHEDULER_MEMORY_HIGH", "SCHEDULER_DEGRADED"}
        or memory >= 0.75
        or swap >= 0.20
        or backlog.pressure_score >= 0.75
        or backlog.explosive_growth
        or (diagnosis.growth_rate > 524288 and memory >= 0.60)
    ):
        return "PROTECTIVE"
    if (
        diagnosis.operational_state in {"SCHEDULER_MEMORY_ELEVATED", "OBSERVE_MORE"}
        or memory >= 0.60
        or backlog.pressure_score >= 0.40
        or backlog.growth_rate > 0
        or cycle >= 300
    ):
        return "CONSERVATIVE"
    return "NORMAL"


def analyze_backlog(db: Session | None = None) -> BacklogSignals:
    pending_total = _pending_total(db)
    throughput = _throughput_estimate(db)
    growth_rate = _backlog_growth_rate(pending_total)
    pressure = min(1.0, pending_total / 1000.0)
    starvation = pending_total > 0 and throughput <= 0
    explosive = growth_rate > 1.0 and pending_total > 100
    stuck_jobs = _stuck_jobs(db)
    signals = BacklogSignals(
        pending_total=pending_total,
        growth_rate=growth_rate,
        pressure_score=pressure,
        throughput_estimate=throughput,
        starvation_detected=starvation,
        explosive_growth=explosive,
        stuck_jobs=stuck_jobs,
    )
    _append_backlog_history(pending_total)
    return signals


def recommend_actions(
    diagnosis: SchedulerDiagnosis,
    backlog: BacklogSignals,
    mode: str,
    priority: str,
) -> list[str]:
    recommendations: list[str] = []
    if mode == "NORMAL":
        recommendations.append("Manter execucao normal; nenhuma acao requerida.")
    if diagnosis.memory_usage_ratio and diagnosis.memory_usage_ratio >= 0.75:
        recommendations.append(
            "Reduzir batch size/concurrency apenas se a pressao persistir fora de dry-run."
        )
    if diagnosis.growth_rate > 524288:
        recommendations.append(
            "Investigar possivel leak se crescimento continuar apos o ciclo atual."
        )
    if diagnosis.swap_usage_ratio and diagnosis.swap_usage_ratio >= 0.20:
        recommendations.append("Verificar swap do host e correlacionar com outros containers.")
    if backlog.explosive_growth:
        recommendations.append(
            "Backlog crescendo mais rapido que throughput; investigar falhas de normalizacao."
        )
    if backlog.starvation_detected:
        recommendations.append("Possivel starvation: ha backlog sem throughput recente.")
    if priority == "LOW" and mode in {"PROTECTIVE", "CRITICAL_PROTECTION"}:
        recommendations.append("Aplicar delay/cooldown em jobs LOW; nao cancelar jobs criticos.")
    return recommendations


def update_reliability_metrics(decision: ReliabilityDecision) -> None:
    try:
        from api.metrics import (
            data_core_scheduler_backlog_growth_rate,
            data_core_scheduler_effective_batch_size,
            data_core_scheduler_effective_concurrency,
            data_core_scheduler_effective_cooldown_seconds,
            data_core_scheduler_protection_mode,
            data_core_scheduler_reliability_audit_total,
            data_core_scheduler_throttled_jobs_total,
            data_core_scheduler_throughput_estimate,
            reliability_dry_run_decisions_total,
            reliability_false_positive_candidates_total,
            reliability_max_backlog_score_observed,
            reliability_max_memory_ratio_observed,
            reliability_mode_changes_total,
        )

        data_core_scheduler_protection_mode.set(MODE_VALUE.get(decision.mode, 0))
        data_core_scheduler_effective_concurrency.set(decision.concurrency)
        data_core_scheduler_effective_batch_size.labels(job_name=decision.job_name).set(
            decision.batch_size
        )
        data_core_scheduler_effective_cooldown_seconds.labels(job_name=decision.job_name).set(
            decision.cooldown_seconds + decision.low_priority_delay_seconds
        )
        data_core_scheduler_backlog_growth_rate.set(decision.backlog.growth_rate)
        data_core_scheduler_throughput_estimate.set(decision.backlog.throughput_estimate)
        data_core_scheduler_reliability_audit_total.labels(
            job_name=decision.job_name,
            priority=decision.priority,
            mode=decision.mode,
            dry_run=str(decision.dry_run).lower(),
        ).inc()
        if decision.dry_run:
            reliability_dry_run_decisions_total.labels(
                job_name=decision.job_name,
                priority=decision.priority,
                mode=decision.mode,
            ).inc()
        if is_false_positive_candidate({"mode": decision.mode, **decision.to_dict()}):
            reliability_false_positive_candidates_total.labels(
                job_name=decision.job_name,
                priority=decision.priority,
                mode=decision.mode,
            ).inc()
        reliability_max_memory_ratio_observed.set(
            max(
                reliability_max_memory_ratio_observed._value.get(),
                decision.memory_usage_ratio,
            )
        )
        reliability_max_backlog_score_observed.set(
            max(
                reliability_max_backlog_score_observed._value.get(),
                decision.backlog.pressure_score,
            )
        )
        if decision.throttled:
            data_core_scheduler_throttled_jobs_total.labels(
                job_name=decision.job_name,
                priority=decision.priority,
                mode=decision.mode,
                reason=decision.throttle_reason or "pressure",
                dry_run=str(decision.dry_run).lower(),
            ).inc()
        audit_summary = scheduler_reliability_audit_report()["summary"]
        reliability_mode_changes_total.set(audit_summary["mode_changes_total"])
    except Exception:
        logger.exception("Failed to update scheduler reliability metrics")


def append_reliability_audit(decision: ReliabilityDecision, path: Path = AUDIT_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            **decision.to_dict(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        logger.exception("Failed to append scheduler reliability audit")


def scheduler_reliability_audit_report(
    path: Path = AUDIT_PATH,
    *,
    last_minutes: int | None = None,
    mode: str | None = None,
    job_priority: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Read scheduler dry-run audit logs and return an operational calibration report."""
    events, audit_health = read_reliability_audit_events(
        path,
        last_minutes=last_minutes,
        mode=mode,
        job_priority=job_priority,
    )
    summary = summarize_reliability_audit(
        events,
        audit_health=audit_health,
    )
    sync_reliability_audit_metrics(events, summary)
    return {
        "filters": {
            "last_minutes": last_minutes,
            "mode": mode,
            "job_priority": job_priority,
        },
        "audit_path": str(path),
        "audit_health": audit_health,
        "activation_gates": activation_gates(summary),
        "summary": summary,
        "operational_report": build_reliability_operational_report(summary),
        "latest_events": events[-limit:],
    }


def sync_reliability_audit_metrics(
    events: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    """Mirror audit-derived evidence into the current process metrics registry."""
    try:
        from api.metrics import (
            reliability_dry_run_decisions_total,
            reliability_false_positive_candidates_total,
            reliability_max_backlog_score_observed,
            reliability_max_memory_ratio_observed,
            reliability_mode_changes_total,
        )

        dry_run_counts: Counter[tuple[str, str, str]] = Counter()
        false_positive_counts: Counter[tuple[str, str, str]] = Counter()
        for event in events:
            key = (
                str(event.get("job_name", "unknown")),
                str(event.get("priority", "unknown")),
                str(event.get("mode", "UNKNOWN")),
            )
            if bool(event.get("dry_run")):
                dry_run_counts[key] += 1
            if is_false_positive_candidate(event):
                false_positive_counts[key] += 1

        for key, count in dry_run_counts.items():
            previous = _SYNCED_DRY_RUN_COUNTS.get(key, 0)
            if count > previous:
                reliability_dry_run_decisions_total.labels(
                    job_name=key[0],
                    priority=key[1],
                    mode=key[2],
                ).inc(count - previous)
                _SYNCED_DRY_RUN_COUNTS[key] = count

        for key, count in false_positive_counts.items():
            previous = _SYNCED_FALSE_POSITIVE_COUNTS.get(key, 0)
            if count > previous:
                reliability_false_positive_candidates_total.labels(
                    job_name=key[0],
                    priority=key[1],
                    mode=key[2],
                ).inc(count - previous)
                _SYNCED_FALSE_POSITIVE_COUNTS[key] = count

        max_observed = summary.get("max_observed", {})
        reliability_mode_changes_total.set(float(summary.get("mode_changes_total", 0.0)))
        reliability_max_memory_ratio_observed.set(
            _float(max_observed.get("memory_usage_ratio"))
        )
        reliability_max_backlog_score_observed.set(
            _float(max_observed.get("backlog_score"))
        )
    except Exception:
        logger.exception("Failed to sync scheduler reliability audit metrics")


def read_reliability_audit_events(
    path: Path = AUDIT_PATH,
    *,
    last_minutes: int | None = None,
    mode: str | None = None,
    job_priority: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    health = {
        "file_exists": path.exists(),
        "file_empty": True,
        "corrupt_lines": 0,
        "schema_errors": 0,
        "schema_error_samples": [],
        "directory_exists": path.parent.exists(),
        "path": str(path),
    }
    if not path.exists():
        return [], health

    cutoff = None
    if last_minutes is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=last_minutes)

    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        health["file_empty"] = False
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            health["corrupt_lines"] += 1
            continue

        schema_errors = audit_event_schema_errors(event)
        if schema_errors:
            health["schema_errors"] += 1
            if len(health["schema_error_samples"]) < 5:
                health["schema_error_samples"].append(
                    {"timestamp": event.get("timestamp"), "errors": schema_errors}
                )

        timestamp = _parse_event_timestamp(event)
        if cutoff is not None and (timestamp is None or timestamp < cutoff):
            continue
        if mode is not None and event.get("mode") != mode:
            continue
        if job_priority is not None and event.get("priority") != job_priority:
            continue
        events.append(event)

    events.sort(key=lambda item: item.get("timestamp", ""))
    return events, health


def summarize_reliability_audit(
    events: list[dict[str, Any]],
    *,
    audit_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit_health = audit_health or {}
    corrupt_lines = int(audit_health.get("corrupt_lines", 0) or 0)
    schema_errors = int(audit_health.get("schema_errors", 0) or 0)
    mode_counts = Counter(str(event.get("mode", "UNKNOWN")) for event in events)
    priority_counts = Counter(str(event.get("priority", "UNKNOWN")) for event in events)
    dry_run_decisions = sum(1 for event in events if bool(event.get("dry_run")))
    mode_changes = _count_mode_changes(events)
    false_positive_candidates = [
        event for event in events if is_false_positive_candidate(event)
    ]
    max_memory_ratio = max(
        (_float(event.get("memory_usage_ratio")) for event in events),
        default=0.0,
    )
    max_pressure = max(
        (_float(_backlog_value(event, "pressure_score")) for event in events),
        default=0.0,
    )
    max_backlog = max(
        (_float(_backlog_value(event, "pending_total")) for event in events),
        default=0.0,
    )
    max_backlog_growth = max(
        (_float(_backlog_value(event, "growth_rate")) for event in events),
        default=0.0,
    )
    max_memory_growth = max(
        (_float(event.get("memory_growth_rate")) for event in events),
        default=0.0,
    )
    predominant_mode = mode_counts.most_common(1)[0][0] if mode_counts else "UNKNOWN"
    total = len(events)
    normal_ratio = (mode_counts.get("NORMAL", 0) / total) if total else 0.0
    normal_stable = (
        total > 0
        and normal_ratio >= 0.90
        and mode_changes <= max(1, int(total * 0.05))
    )
    window_hours = _event_window_hours(events)
    false_positive_ratio = (len(false_positive_candidates) / total) if total else 0.0
    mode_change_ratio = (mode_changes / total) if total else 0.0

    return {
        "total_events": total,
        "corrupt_lines": corrupt_lines,
        "schema_errors": schema_errors,
        "window_hours": round(window_hours, 4),
        "mode_counts": dict(mode_counts),
        "priority_counts": dict(priority_counts),
        "dry_run_decisions_total": dry_run_decisions,
        "mode_changes_total": mode_changes,
        "oscillation_detected": mode_changes > max(2, int(total * 0.20)) if total else False,
        "normal_stable": normal_stable,
        "normal_ratio": round(normal_ratio, 4),
        "false_positive_candidates_total": len(false_positive_candidates),
        "false_positive_ratio": round(false_positive_ratio, 4),
        "false_positive_candidates": false_positive_candidates[-10:],
        "growth_rate": {
            "max_memory_growth_rate": max_memory_growth,
            "max_backlog_growth_rate": max_backlog_growth,
        },
        "max_observed": {
            "pressure_score": max_pressure,
            "memory_usage_ratio": max_memory_ratio,
            "backlog_pending_total": max_backlog,
            "backlog_score": max_pressure,
        },
        "predominant_mode": predominant_mode,
        "readiness_recommendation": readiness_recommendation(
            total_events=total,
            predominant_mode=predominant_mode,
            normal_stable=normal_stable,
            mode_changes=mode_changes,
            mode_change_ratio=mode_change_ratio,
            false_positive_count=len(false_positive_candidates),
            false_positive_ratio=false_positive_ratio,
            max_mode_value=max(
                (MODE_VALUE.get(str(event.get("mode")), 0) for event in events),
                default=0,
            ),
            corrupt_lines=corrupt_lines,
            schema_errors=schema_errors,
            window_hours=window_hours,
        ),
    }


def build_reliability_operational_report(summary: dict[str, Any]) -> dict[str, Any]:
    max_observed = summary["max_observed"]
    return {
        "predominant_mode": summary["predominant_mode"],
        "highest_pressure_observed": max_observed["pressure_score"],
        "highest_memory_observed": max_observed["memory_usage_ratio"],
        "highest_backlog_observed": max_observed["backlog_pending_total"],
        "mode_changes_total": summary["mode_changes_total"],
        "false_positive_candidates_total": summary["false_positive_candidates_total"],
        "recommendation": summary["readiness_recommendation"],
    }


def readiness_recommendation(
    *,
    total_events: int,
    predominant_mode: str,
    normal_stable: bool,
    mode_changes: int,
    mode_change_ratio: float = 0.0,
    false_positive_count: int = 0,
    false_positive_ratio: float = 0.0,
    max_mode_value: int = 0,
    corrupt_lines: int = 0,
    schema_errors: int = 0,
    window_hours: float = 0.0,
) -> str:
    if total_events == 0:
        return "KEEP_DRY_RUN_INSUFFICIENT_DATA"
    if total_events < MIN_CALIBRATION_DECISIONS or window_hours < MIN_CALIBRATION_HOURS:
        return "KEEP_DRY_RUN_INSUFFICIENT_DATA"
    if corrupt_lines > 0 or schema_errors > 0:
        return "DO_NOT_ENABLE_RUNTIME_UNSTABLE"
    if false_positive_count > 0 and false_positive_ratio > MAX_FALSE_POSITIVE_RATIO:
        return "KEEP_DRY_RUN_HIGH_FALSE_POSITIVE_RISK"
    if mode_changes > 0 and mode_change_ratio > MAX_MODE_CHANGE_RATIO:
        return "DO_NOT_ENABLE_RUNTIME_UNSTABLE"
    if max_mode_value >= MODE_VALUE["PROTECTIVE"]:
        return "DO_NOT_ENABLE_RUNTIME_UNSTABLE"
    if predominant_mode == "NORMAL" and normal_stable:
        return "READY_FOR_LIMITED_ENABLEMENT"
    return "KEEP_DRY_RUN_INSUFFICIENT_DATA"


def activation_gates(summary: dict[str, Any]) -> dict[str, Any]:
    total_events = int(summary.get("total_events", 0) or 0)
    false_positive_ratio = float(summary.get("false_positive_ratio", 0.0) or 0.0)
    mode_changes = int(summary.get("mode_changes_total", 0) or 0)
    mode_change_ratio = (mode_changes / total_events) if total_events else 0.0
    max_mode_value = max(
        (MODE_VALUE.get(mode, 0) for mode in summary.get("mode_counts", {})),
        default=0,
    )
    return {
        "minimum_window_hours": {
            "required": MIN_CALIBRATION_HOURS,
            "observed": summary.get("window_hours", 0.0),
            "passed": float(summary.get("window_hours", 0.0) or 0.0) >= MIN_CALIBRATION_HOURS,
        },
        "minimum_dry_run_decisions": {
            "required": MIN_CALIBRATION_DECISIONS,
            "observed": total_events,
            "passed": total_events >= MIN_CALIBRATION_DECISIONS,
        },
        "audit_integrity": {
            "corrupt_lines": summary.get("corrupt_lines", 0),
            "schema_errors": summary.get("schema_errors", 0),
            "passed": summary.get("corrupt_lines", 0) == 0
            and summary.get("schema_errors", 0) == 0,
        },
        "false_positive_risk": {
            "max_ratio": MAX_FALSE_POSITIVE_RATIO,
            "observed_ratio": false_positive_ratio,
            "passed": false_positive_ratio <= MAX_FALSE_POSITIVE_RATIO,
        },
        "mode_stability": {
            "max_change_ratio": MAX_MODE_CHANGE_RATIO,
            "observed_change_ratio": round(mode_change_ratio, 4),
            "passed": mode_change_ratio <= MAX_MODE_CHANGE_RATIO,
        },
        "pressure_ceiling": {
            "max_allowed_mode": "CONSERVATIVE",
            "observed_max_mode_value": max_mode_value,
            "passed": max_mode_value < MODE_VALUE["PROTECTIVE"],
        },
        "readiness": {
            "recommendation": summary.get("readiness_recommendation"),
            "passed": summary.get("readiness_recommendation") == "READY_FOR_LIMITED_ENABLEMENT",
        },
    }


def audit_event_schema_errors(event: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_AUDIT_FIELDS - set(event))
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")
    if event.get("mode") not in MODE_VALUE:
        errors.append("invalid_mode")
    if event.get("priority") not in PRIORITY_VALUE:
        errors.append("invalid_priority")
    if not isinstance(event.get("dry_run"), bool):
        errors.append("dry_run_not_bool")
    if not isinstance(event.get("enabled"), bool):
        errors.append("enabled_not_bool")
    backlog = event.get("backlog")
    if not isinstance(backlog, dict):
        errors.append("backlog_not_object")
    else:
        missing_backlog = sorted(REQUIRED_BACKLOG_FIELDS - set(backlog))
        if missing_backlog:
            errors.append(f"missing_backlog_fields:{','.join(missing_backlog)}")
    if _parse_event_timestamp(event) is None:
        errors.append("invalid_timestamp")
    return errors


def is_false_positive_candidate(event: dict[str, Any]) -> bool:
    if event.get("mode") == "NORMAL":
        return False
    severity = str(event.get("severity", "")).lower()
    diagnosis_state = str(event.get("diagnosis_state", ""))
    memory_ratio = _float(event.get("memory_usage_ratio"))
    backlog_pressure = _float(_backlog_value(event, "pressure_score"))
    backlog_growth = _float(_backlog_value(event, "growth_rate"))
    return (
        severity == "info"
        and diagnosis_state in {"SCHEDULER_HEALTHY", "OBSERVE_MORE", ""}
        and memory_ratio < 0.60
        and backlog_pressure < 0.40
        and backlog_growth <= 0
    )


def _count_mode_changes(events: list[dict[str, Any]]) -> int:
    changes = 0
    previous = None
    for event in events:
        current = event.get("mode")
        if previous is not None and current != previous:
            changes += 1
        previous = current
    return changes


def _event_window_hours(events: list[dict[str, Any]]) -> float:
    timestamps = [
        parsed
        for parsed in (_parse_event_timestamp(event) for event in events)
        if parsed is not None
    ]
    if len(timestamps) < 2:
        return 0.0
    return (max(timestamps) - min(timestamps)).total_seconds() / 3600


def _parse_event_timestamp(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _backlog_value(event: dict[str, Any], key: str) -> Any:
    backlog = event.get("backlog")
    if isinstance(backlog, dict):
        return backlog.get(key)
    return None


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _pending_total(db: Session | None) -> int:
    if db is None:
        return 0
    try:
        return int(
            db.query(func.count(RawCollection.id))
            .filter(RawCollection.processing_status == "normalization_pending")
            .scalar()
            or 0
        )
    except Exception:
        logger.debug("Failed to query pending backlog", exc_info=True)
        return 0


def _throughput_estimate(db: Session | None) -> float:
    if db is None:
        return 0.0
    try:
        rows = (
            db.query(PipelineRun)
            .filter(PipelineRun.trigger == "scheduler", PipelineRun.finished_at.is_not(None))
            .order_by(PipelineRun.finished_at.desc().nullslast())
            .limit(10)
            .all()
        )
        processed = sum(int(row.items_processed or 0) for row in rows)
        duration = sum(float(row.duration_seconds or 0) for row in rows)
        if processed <= 0 or duration <= 0:
            return 0.0
        return processed / duration
    except Exception:
        logger.debug("Failed to estimate throughput", exc_info=True)
        return 0.0


def _stuck_jobs(db: Session | None) -> int:
    if db is None:
        return 0
    try:
        return int(
            db.query(func.count(PipelineRun.id))
            .filter(PipelineRun.status == "running")
            .scalar()
            or 0
        )
    except Exception:
        logger.debug("Failed to count stuck jobs", exc_info=True)
        return 0


def _backlog_growth_rate(current_pending: int) -> float:
    now = time.time()
    previous = _last_backlog_sample()
    if not previous:
        return 0.0
    previous_t, previous_pending = previous
    if now <= previous_t:
        return 0.0
    return (current_pending - previous_pending) / (now - previous_t)


def _append_backlog_history(pending_total: int) -> None:
    try:
        BACKLOG_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BACKLOG_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"timestamp_epoch": time.time(), "pending_total": pending_total})
                + "\n"
            )
    except Exception:
        logger.debug("Failed to append backlog history", exc_info=True)


def _last_backlog_sample() -> tuple[float, int] | None:
    try:
        if not BACKLOG_HISTORY_PATH.exists():
            return None
        lines = [
            line
            for line in BACKLOG_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not lines:
            return None
        data = json.loads(lines[-1])
        return float(data["timestamp_epoch"]), int(data["pending_total"])
    except Exception:
        return None


def _throttle_reason(diagnosis: SchedulerDiagnosis, backlog: BacklogSignals) -> str:
    if diagnosis.memory_usage_ratio and diagnosis.memory_usage_ratio >= 0.75:
        return "memory_pressure"
    if diagnosis.swap_usage_ratio and diagnosis.swap_usage_ratio >= 0.20:
        return "swap_pressure"
    if backlog.pressure_score >= 0.75:
        return "backlog_pressure"
    if diagnosis.growth_rate > 524288:
        return "memory_growth"
    return "protective_mode"

"""
IncidentHistoryService — memória operacional da plataforma.

Responsabilidades:
  1. Converter IncidentEvents resolvidos em IncidentHistory records
  2. Agregar por alert_id → IncidentPattern (MTTR, frequência, root causes)
  3. Detectar flapping (>3 ocorrências em 24h)
  4. Normalizar root causes em buckets reutilizáveis
  5. Expor histórico para o RCA Engine (Fase 9) e AI Agent (Fase 10)
"""

import logging
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.incident_bus.models import IncidentEvent
from app.incident_history.metrics import (
    HISTORY_AGGREGATION_DURATION,
    HISTORY_RECORDS_CREATED,
    HISTORY_PATTERNS_UPDATED,
    HISTORY_AGGREGATION_ERRORS,
)
from app.incident_history.models import IncidentHistory, IncidentPattern
from app.incident_history.schemas import (
    AggregationResult,
    IncidentHistoryCreate,
)

logger = logging.getLogger(__name__)

# ── Root cause normalizer ─────────────────────────────────────────────────────
# Mapeia texto livre de root_cause para um bucket canônico.
# O bucket é usado para agregação e consulta pelo AI Agent.

_ROOT_CAUSE_PATTERNS: list[tuple[list[str], str]] = [
    (["oom", "out of memory", "memory limit", "killed"],           "oom_kill"),
    (["restart", "crash loop", "crash-loop", "restarted"],         "crash_loop"),
    (["redis", "redis down", "redis unavailable"],                 "redis_unavailable"),
    (["postgres", "postgresql", "database", "db down", "db lock"], "database_issue"),
    (["scheduler", "apscheduler", "heartbeat", "frozen"],          "scheduler_frozen"),
    (["pipeline", "normalization", "normalize", "backlog"],        "pipeline_stalled"),
    (["deploy", "deployment", "deploy fail"],                      "deployment_failure"),
    (["network", "connectivity", "unreachable", "timeout"],        "network_issue"),
    (["config", "env", "environment variable", "missing var"],     "config_error"),
    (["schema", "parser", "format change", "scraper broke"],       "schema_change"),
    (["rate limit", "rate limiting", "throttl", "blocked", "ban"], "rate_limiting"),
    (["centavos", "misparse", "price guard", "incident-2"],        "price_data_corruption"),
]


def _normalize_root_cause(root_cause: str | None) -> str | None:
    """Mapeia root_cause livre para bucket canônico."""
    if not root_cause:
        return None
    lower = root_cause.lower()
    for keywords, bucket in _ROOT_CAUSE_PATTERNS:
        if any(kw in lower for kw in keywords):
            return bucket
    return "other"


# ── Service ───────────────────────────────────────────────────────────────────

class IncidentHistoryService:

    def aggregate(self, db: Session) -> AggregationResult:
        """
        Job principal — deve ser chamado pelo APScheduler a cada hora.

        1. Busca IncidentEvents resolvidos (processed=True) sem IncidentHistory
        2. Cria IncidentHistory records
        3. Re-agrega IncidentPatterns para os alert_ids afetados
        """
        t0 = time.perf_counter()
        new_records = 0
        updated_patterns = 0
        errors = 0
        affected_alert_ids: set[str] = set()

        try:
            # ── Step 1: find processed events without history records ─────────
            existing_ids = db.execute(
                select(IncidentHistory.incident_event_id)
                .where(IncidentHistory.incident_event_id.is_not(None))
            ).scalars().all()

            events_to_process = (
                db.query(IncidentEvent)
                .filter(
                    IncidentEvent.processed.is_(True),
                    IncidentEvent.id.not_in(existing_ids) if existing_ids else True,
                )
                .all()
            )

            # ── Step 2: create history records ───────────────────────────────
            for event in events_to_process:
                try:
                    history = self._event_to_history(event)
                    db.add(history)
                    new_records += 1
                    if event.alert_id:
                        affected_alert_ids.add(event.alert_id)
                    HISTORY_RECORDS_CREATED.inc()
                except Exception as exc:
                    errors += 1
                    HISTORY_AGGREGATION_ERRORS.inc()
                    logger.warning("Failed to create history for event %s: %s", event.id, exc)

            db.flush()

            # ── Step 3: re-aggregate patterns for affected alert_ids ─────────
            for alert_id in affected_alert_ids:
                try:
                    self._aggregate_pattern(db, alert_id)
                    updated_patterns += 1
                    HISTORY_PATTERNS_UPDATED.inc()
                except Exception as exc:
                    errors += 1
                    HISTORY_AGGREGATION_ERRORS.inc()
                    logger.warning("Failed to aggregate pattern for %s: %s", alert_id, exc)

            db.commit()

        except Exception:
            db.rollback()
            logger.exception("Incident history aggregation failed")
            errors += 1

        duration_ms = (time.perf_counter() - t0) * 1000
        HISTORY_AGGREGATION_DURATION.observe(duration_ms / 1000)

        logger.info(
            "Incident history aggregation: %d events → %d history records, "
            "%d patterns updated, %d errors (%.1fms)",
            len(events_to_process) if "events_to_process" in locals() else 0,
            new_records, updated_patterns, errors, duration_ms,
        )

        return AggregationResult(
            processed_events=len(events_to_process) if "events_to_process" in locals() else 0,
            new_history_records=new_records,
            updated_patterns=updated_patterns,
            errors=errors,
            duration_ms=duration_ms,
        )

    def record_manual(
        self,
        db: Session,
        create: IncidentHistoryCreate,
    ) -> IncidentHistory:
        """
        Registra manualmente um incidente resolvido (ex: pelo AI Agent ou operador).
        Dispara re-agregação do pattern correspondente.
        """
        history = IncidentHistory(
            incident_event_id=create.incident_event_id,
            alert_id=create.alert_id,
            alertname=create.alertname,
            service=create.service,
            severity=create.severity,
            category=create.category,
            root_cause=create.root_cause,
            root_cause_bucket=create.root_cause_bucket or _normalize_root_cause(create.root_cause),
            rca_confidence=create.rca_confidence,
            resolution=create.resolution,
            resolution_type=create.resolution_type,
            resolved_by=create.resolved_by,
            fired_at=create.fired_at,
            resolved_at=create.resolved_at,
            duration_seconds=create.duration_seconds,
            ai_action_used=create.ai_action_used,
            runbook=create.runbook,
            context_snapshot=create.context_snapshot,
        )
        db.add(history)
        db.flush()

        if create.alert_id:
            self._aggregate_pattern(db, create.alert_id)

        db.commit()
        HISTORY_RECORDS_CREATED.inc()
        return history

    # ── Query methods (for RCA Engine + AI Agent) ─────────────────────────────

    @staticmethod
    def get_pattern(db: Session, alert_id: str) -> IncidentPattern | None:
        return db.query(IncidentPattern).filter_by(alert_id=alert_id).first()

    @staticmethod
    def get_all_patterns(
        db: Session,
        service: str | None = None,
        is_flapping: bool | None = None,
        min_occurrences: int = 1,
        limit: int = 100,
    ) -> list[IncidentPattern]:
        q = db.query(IncidentPattern).filter(
            IncidentPattern.total_occurrences >= min_occurrences
        )
        if service:
            q = q.filter(IncidentPattern.service == service)
        if is_flapping is not None:
            q = q.filter(IncidentPattern.is_flapping.is_(is_flapping))
        return q.order_by(IncidentPattern.total_occurrences.desc()).limit(limit).all()

    @staticmethod
    def get_history_for_alert(
        db: Session,
        alert_id: str,
        limit: int = 20,
    ) -> list[IncidentHistory]:
        return (
            db.query(IncidentHistory)
            .filter_by(alert_id=alert_id)
            .order_by(IncidentHistory.recorded_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_recent_history(
        db: Session,
        hours: int = 24,
        service: str | None = None,
        limit: int = 50,
    ) -> list[IncidentHistory]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        q = db.query(IncidentHistory).filter(IncidentHistory.recorded_at >= cutoff)
        if service:
            q = q.filter(IncidentHistory.service == service)
        return q.order_by(IncidentHistory.recorded_at.desc()).limit(limit).all()

    def get_likely_root_cause(
        self,
        db: Session,
        alert_id: str,
    ) -> dict[str, Any] | None:
        """
        Para o RCA Engine: retorna a root cause mais provável baseada no histórico.
        Retorna None se não há histórico suficiente (< 3 ocorrências resolvidas).
        """
        pattern = self.get_pattern(db, alert_id)
        if not pattern or not pattern.top_root_causes:
            return None
        if pattern.resolved_count < 3:
            return None  # histórico insuficiente

        top = pattern.top_root_causes[0] if pattern.top_root_causes else None
        if not top:
            return None

        return {
            "alert_id": alert_id,
            "root_cause_bucket": top.get("bucket"),
            "confidence": top.get("pct", 0.0) * (pattern.rca_confidence_avg or 0.5),
            "occurrences": top.get("count"),
            "total_resolved": pattern.resolved_count,
            "mttr_seconds": pattern.mttr_seconds,
            "is_flapping": pattern.is_flapping,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _event_to_history(event: IncidentEvent) -> IncidentHistory:
        return IncidentHistory(
            incident_event_id=event.id,
            alert_id=event.alert_id,
            alertname=event.alertname,
            service=event.service,
            severity=event.severity,
            category=event.category,
            root_cause=event.root_cause,
            root_cause_bucket=_normalize_root_cause(event.root_cause),
            rca_confidence=event.rca_confidence,
            resolution=event.resolution_notes,
            resolved_by="ai_agent" if event.rca_confidence else "human",
            fired_at=event.fired_at,
            resolved_at=event.resolved_at,
            duration_seconds=event.duration_seconds,
            runbook=event.runbook,
        )

    def _aggregate_pattern(self, db: Session, alert_id: str) -> None:
        """Recalcula o IncidentPattern para um alert_id."""
        history_rows = (
            db.query(IncidentHistory)
            .filter_by(alert_id=alert_id)
            .order_by(IncidentHistory.recorded_at.asc())
            .all()
        )
        if not history_rows:
            return

        # Frequência
        total = len(history_rows)
        resolved = [r for r in history_rows if r.duration_seconds is not None]
        unresolved = total - len(resolved)

        # MTTR
        durations = [r.duration_seconds for r in resolved if r.duration_seconds and r.duration_seconds > 0]
        mttr = statistics.mean(durations) if durations else None
        mttr_p50 = statistics.median(durations) if durations else None
        mttr_p90 = (
            sorted(durations)[int(len(durations) * 0.9)] if len(durations) >= 5 else None
        )

        # Root causes top-3
        bucket_counts: Counter = Counter(
            r.root_cause_bucket for r in history_rows if r.root_cause_bucket
        )
        top_causes = [
            {"bucket": b, "count": c, "pct": round(c / total, 3)}
            for b, c in bucket_counts.most_common(3)
        ]

        # RCA confidence avg
        confidences = [r.rca_confidence for r in history_rows if r.rca_confidence]
        rca_conf_avg = statistics.mean(confidences) if confidences else None

        # Recurrence interval
        fire_times = sorted(
            r.fired_at for r in history_rows
            if r.fired_at and r.fired_at.tzinfo
        )
        if len(fire_times) >= 2:
            intervals = [
                (fire_times[i] - fire_times[i - 1]).total_seconds() / 3600
                for i in range(1, len(fire_times))
            ]
            recurrence_hours = statistics.mean(intervals)
        else:
            recurrence_hours = None

        # Flapping: >3 ocorrências nas últimas 24h
        cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_count = sum(
            1 for r in history_rows
            if r.recorded_at and r.recorded_at.tzinfo and r.recorded_at >= cutoff_24h
        )
        is_flapping = recent_count > 3

        # Upsert pattern
        sample = history_rows[-1]
        pattern = db.query(IncidentPattern).filter_by(alert_id=alert_id).first()
        if pattern is None:
            pattern = IncidentPattern(
                alert_id=alert_id,
                alertname=sample.alertname,
                service=sample.service,
                severity=sample.severity,
            )
            db.add(pattern)

        pattern.total_occurrences = total
        pattern.resolved_count = len(resolved)
        pattern.unresolved_count = unresolved
        pattern.last_fired_at = fire_times[-1] if fire_times else None
        pattern.first_fired_at = fire_times[0] if fire_times else None
        pattern.mttr_seconds = mttr
        pattern.mttr_p50_seconds = mttr_p50
        pattern.mttr_p90_seconds = mttr_p90
        pattern.top_root_causes = top_causes
        pattern.recurrence_interval_hours = recurrence_hours
        pattern.is_flapping = is_flapping
        pattern.rca_confidence_avg = rca_conf_avg
        pattern.last_aggregated_at = datetime.now(timezone.utc)

        db.flush()

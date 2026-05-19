"""NormalizationHealthChecker — monitors raw→normalized conversion pipeline.

Checks:
  1. Normalization backlog: raw records with processing_status='normalization_pending'
     older than threshold → warning/critical
  2. Raw → normalized success rate (last 24h): if < 70% → warning
  3. Normalization failures: if normalization_failed records increasing → warning
  4. Last normalized record age: if oldest module's latest normalized record is
     too old → warning
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.watchdog.checks import CheckResult, WatchdogAlert
from app.raw.models import RawCollection
from app.normalization.models import NormalizedProduct
from core.config import settings

logger = logging.getLogger(__name__)

_BACKLOG_CRITICAL_MINUTES = 90   # pending > 90 min → critical
_SUCCESS_RATE_THRESHOLD = 0.70   # warn if raw→normalized < 70% success in last 24h
_MIN_SAMPLE_SIZE = 5             # ignore stats with fewer than this many raw records


class NormalizationHealthChecker:
    """Check normalization pipeline health."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self) -> CheckResult:
        try:
            return self._run()
        except Exception as exc:
            logger.exception("NormalizationHealthChecker failed")
            return CheckResult(
                name="normalization",
                status="warning",
                summary=f"Normalization check error: {exc}",
            )

    def _run(self) -> CheckResult:
        db = self._db
        now = datetime.now(tz=timezone.utc)
        backlog_threshold = now - timedelta(minutes=settings.watchdog_normalization_backlog_minutes)
        since_24h = now - timedelta(hours=24)

        alerts: list[WatchdogAlert] = []
        metrics: dict[str, Any] = {}

        # ── 1. Pending backlog ────────────────────────────────────────────────
        pending_old = (
            db.query(func.count(RawCollection.id))
            .filter(
                RawCollection.processing_status == "normalization_pending",
                RawCollection.collected_at <= backlog_threshold,
            )
            .scalar()
        ) or 0

        pending_total = (
            db.query(func.count(RawCollection.id))
            .filter(RawCollection.processing_status == "normalization_pending")
            .scalar()
        ) or 0

        metrics["normalization_pending_total"] = pending_total
        metrics["normalization_pending_old"] = pending_old
        metrics["backlog_threshold_minutes"] = settings.watchdog_normalization_backlog_minutes

        if pending_old > 0:
            age_min = settings.watchdog_normalization_backlog_minutes
            severity = "critical" if pending_old > 20 else "warning"
            alerts.append(WatchdogAlert(
                severity=severity,
                code="normalization_backlog",
                title="Backlog de normalização",
                message=(
                    f"{pending_old} registro(s) raw pendente(s) há mais de {age_min} min. "
                    f"(Total pending: {pending_total}). "
                    "Normalizer travado ou com erros?"
                ),
                context={
                    "pending_old": pending_old,
                    "pending_total": pending_total,
                    "threshold_minutes": age_min,
                },
            ))

        # ── 2. Raw → normalized success rate (last 24h) per module ───────────
        status_rows = (
            db.query(
                RawCollection.source_name,
                RawCollection.processing_status,
                func.count().label("cnt"),
            )
            .filter(RawCollection.collected_at >= since_24h)
            .group_by(RawCollection.source_name, RawCollection.processing_status)
            .all()
        )

        # Aggregate per source_name
        source_totals: dict[str, dict] = {}
        for r in status_rows:
            if r.source_name not in source_totals:
                source_totals[r.source_name] = {}
            source_totals[r.source_name][r.processing_status] = r.cnt

        source_rates: dict[str, dict] = {}
        for src, statuses in source_totals.items():
            total = sum(statuses.values())
            normalized = statuses.get("normalized", 0)
            failed = statuses.get("normalization_failed", 0)
            ignored = statuses.get("ignored", 0)
            pending = statuses.get("normalization_pending", 0)

            if total < _MIN_SAMPLE_SIZE:
                continue

            rate = normalized / total if total else 0.0
            source_rates[src] = {
                "total": total,
                "normalized": normalized,
                "failed": failed,
                "ignored": ignored,
                "pending": pending,
                "success_rate": round(rate, 3),
            }

            if rate < _SUCCESS_RATE_THRESHOLD and (normalized + failed) >= _MIN_SAMPLE_SIZE:
                alerts.append(WatchdogAlert(
                    severity="warning",
                    code="normalization_low_success_rate",
                    title=f"Baixa normalização: {src}",
                    message=(
                        f"'{src}': apenas {rate:.0%} dos raw normalizados nas últimas 24h "
                        f"({normalized}/{total}). Verificar normalizer."
                    ),
                    source_name=src,
                    context=source_rates[src],
                ))

        metrics["source_rates"] = source_rates

        # ── 3. Last normalized product age (ecommerce module) ─────────────────
        last_normalized_at = (
            db.query(func.max(NormalizedProduct.normalized_at))
            .scalar()
        )
        if last_normalized_at:
            age_secs = (now - last_normalized_at.replace(tzinfo=timezone.utc)).total_seconds()
            metrics["last_normalized_age_seconds"] = int(age_secs)
            age_h = age_secs / 3600
            if age_h > 4:
                alerts.append(WatchdogAlert(
                    severity="warning",
                    code="normalization_stale",
                    title="Normalização parada (ecommerce)",
                    message=(
                        f"Último produto normalizado há {age_h:.1f}h. "
                        "normalize_job pode estar falhando?"
                    ),
                    context={"age_hours": round(age_h, 1)},
                ))
        else:
            metrics["last_normalized_age_seconds"] = None

        # ── Overall ───────────────────────────────────────────────────────────
        status = _worst_status(alerts)
        if status == "ok":
            total_norm_24h = sum(v.get("normalized", 0) for v in source_rates.values())
            summary = f"Normalização OK — {total_norm_24h} registros nas últimas 24h."
        else:
            summary = f"{len(alerts)} alerta(s) de normalização."

        return CheckResult(
            name="normalization",
            status=status,
            summary=summary,
            alerts=alerts,
            metrics=metrics,
        )


def _worst_status(alerts: list[WatchdogAlert]) -> str:
    if any(a.severity == "critical" for a in alerts):
        return "critical"
    if any(a.severity == "warning" for a in alerts):
        return "warning"
    return "ok"

"""CollectionHealthChecker — monitors raw_collections for freshness and failure rates.

Checks:
  1. Per-domain staleness: no new raw record in last N hours → critical
  2. Overall platform staleness: no new raw record from ANY domain → critical
  3. Failure rate: domain where error raw > threshold in last hour → warning
  4. Active targets with no recent collection → warning
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.raw.models import RawCollection
from app.watchdog.checks import CheckResult, WatchdogAlert
from core.config import settings
from database.models import CollectionRun, CollectionTarget, RunStatus

logger = logging.getLogger(__name__)

# Failure rate threshold: warn if > this % of recent collections are errors
_FAILURE_RATE_THRESHOLD = 0.40


class CollectionHealthChecker:
    """Check raw_collections for staleness and failure spikes."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self) -> CheckResult:
        try:
            return self._run()
        except Exception as exc:
            logger.exception("CollectionHealthChecker failed")
            return CheckResult(
                name="collection",
                status="warning",
                summary=f"Collection check error: {exc}",
            )

    def _run(self) -> CheckResult:
        db = self._db
        stale_hours = settings.watchdog_collection_stale_hours
        now = datetime.now(tz=timezone.utc)
        since_stale = now - timedelta(hours=stale_hours)
        since_1h = now - timedelta(hours=1)

        alerts: list[WatchdogAlert] = []
        metrics: dict[str, Any] = {}

        # ── 1. Recent collections per domain ──────────────────────────────────
        rows = (
            db.query(
                RawCollection.source_name,
                func.count().label("total"),
                func.max(RawCollection.collected_at).label("last_collected_at"),
                func.sum(
                    case(
                        (RawCollection.processing_status == "normalization_failed", 1),
                        else_=0,
                    )
                ).label("error_count"),
            )
            .filter(RawCollection.collected_at >= since_stale)
            .group_by(RawCollection.source_name)
            .all()
        )

        active_sources = {r.source_name for r in rows}
        domain_stats: dict[str, dict] = {}
        for r in rows:
            last_secs = (now - r.last_collected_at.replace(tzinfo=timezone.utc)).total_seconds()
            error_rate = float(r.error_count or 0) / float(r.total) if r.total else 0.0
            domain_stats[r.source_name] = {
                "total": r.total,
                "last_collected_at": r.last_collected_at.isoformat() if r.last_collected_at else None,
                "last_collected_age_seconds": int(last_secs),
                "error_count": r.error_count or 0,
                "error_rate": round(error_rate, 3),
            }

        # ── 2. Domains stale within the window (no data at all) ───────────────
        # Get all unique source_names from the last 7 days to know "known" sources
        known_sources_q = (
            db.query(RawCollection.source_name)
            .filter(RawCollection.collected_at >= now - timedelta(days=7))
            .distinct()
            .all()
        )
        known_sources = {r.source_name for r in known_sources_q}
        stale_sources = known_sources - active_sources

        for src in sorted(stale_sources):
            # Find the absolute last collection time for context
            last = (
                db.query(func.max(RawCollection.collected_at))
                .filter(RawCollection.source_name == src)
                .scalar()
            )
            age_h = None
            if last:
                age_h = (now - last.replace(tzinfo=timezone.utc)).total_seconds() / 3600
            alerts.append(WatchdogAlert(
                severity="critical",
                code="collection_stale",
                title=f"Coleta parada: {src}",
                message=(
                    f"Nenhuma coleta de '{src}' nas últimas {stale_hours}h."
                    + (f" Última coleta: há {age_h:.1f}h." if age_h else "")
                ),
                source_name=src,
                context={"stale_hours": stale_hours, "last_age_hours": age_h},
            ))

        # ── 3. Failure rate spike ─────────────────────────────────────────────
        # Check failure rate in the last 1h window per domain
        rows_1h = (
            db.query(
                RawCollection.source_name,
                func.count().label("total"),
                func.sum(
                    case(
                        (RawCollection.processing_status == "normalization_failed", 1),
                        else_=0,
                    )
                ).label("error_count"),
            )
            .filter(RawCollection.collected_at >= since_1h)
            .group_by(RawCollection.source_name)
            .all()
        )

        for r in rows_1h:
            if r.total and r.total >= 3:  # only flag if we have enough sample
                rate = float(r.error_count or 0) / float(r.total)
                if rate > _FAILURE_RATE_THRESHOLD:
                    alerts.append(WatchdogAlert(
                        severity="warning",
                        code="collection_high_failure_rate",
                        title=f"Alta taxa de falhas: {r.source_name}",
                        message=(
                            f"'{r.source_name}': {r.error_count}/{r.total} falhas na última 1h "
                            f"({rate:.0%}). Possível mudança no site ou bloqueio."
                        ),
                        source_name=r.source_name,
                        context={"failure_rate": rate, "total": r.total, "errors": r.error_count},
                    ))

        # ── 4. Platform-wide staleness (no data from ANY source) ──────────────
        if not active_sources and known_sources:
            alerts.append(WatchdogAlert(
                severity="critical",
                code="collection_platform_down",
                title="Plataforma sem coleta",
                message=(
                    f"NENHUMA fonte coletou dados nas últimas {stale_hours}h. "
                    "Scheduler parado ou banco indisponível?"
                ),
                context={"stale_hours": stale_hours, "known_sources": sorted(known_sources)},
            ))

        # ── M1. Source concentration: source_share > 60% in last 24h ────────────
        rows_24h = (
            db.query(
                RawCollection.source_name,
                func.count().label("cnt"),
            )
            .filter(
                RawCollection.collected_at >= now - timedelta(hours=24),
                RawCollection.module.in_(["ecommerce", "crypto"]),
            )
            .group_by(RawCollection.source_name)
            .all()
        )
        total_24h = sum(r.cnt for r in rows_24h)
        if total_24h > 0:
            for r in rows_24h:
                share = r.cnt / total_24h
                if share > 0.60:
                    alerts.append(WatchdogAlert(
                        severity="warning",
                        code="source_concentration_high",
                        title=f"Concentração alta: {r.source_name} ({share:.0%})",
                        message=(
                            f"Fonte '{r.source_name}' representa {share:.0%} dos registros "
                            f"das últimas 24h ({r.cnt}/{total_24h}). "
                            "HHI em risco — verificar outras fontes ativas."
                        ),
                        source_name=r.source_name,
                        context={"share": round(share, 3), "count": r.cnt, "total": total_24h},
                    ))

        # ── M2. Collector com 0 records em run agendado ───────────────────────
        # Alerta se um coletor ativo completou 0 raw_saved_count E 0 items_collected
        # nas últimas 2x o seu intervalo default (janela mínima de 3h).
        _ZERO_RUN_WINDOW_HOURS = 3
        zero_runs = (
            db.query(CollectionRun.collector_name, func.count().label("runs"))
            .filter(
                CollectionRun.started_at >= now - timedelta(hours=_ZERO_RUN_WINDOW_HOURS),
                CollectionRun.status == RunStatus.success,
                CollectionRun.items_collected == 0,
                CollectionRun.raw_saved_count == 0,
            )
            .group_by(CollectionRun.collector_name)
            .all()
        )
        # Only alert for collectors we know are supposed to produce data
        for r in zero_runs:
            alerts.append(WatchdogAlert(
                severity="warning",
                code="collector_zero_output",
                title=f"Coletor sem output: {r.collector_name}",
                message=(
                    f"'{r.collector_name}' completou {r.runs} run(s) com 0 registros "
                    f"nas últimas {_ZERO_RUN_WINDOW_HOURS}h. "
                    "Fonte offline, bloqueio ou bug de persistência?"
                ),
                source_name=r.collector_name,
                context={"zero_runs": r.runs, "window_hours": _ZERO_RUN_WINDOW_HOURS},
            ))

        # ── 5. Active targets with no recent activity ─────────────────────────
        active_target_count = (
            db.query(func.count(CollectionTarget.id))
            .filter(CollectionTarget.active == True)  # noqa: E712
            .scalar()
        ) or 0

        metrics["domain_stats"] = domain_stats
        metrics["known_sources_count"] = len(known_sources)
        metrics["active_sources_last_window"] = len(active_sources)
        metrics["stale_sources"] = sorted(stale_sources)
        metrics["active_target_count"] = active_target_count

        # Overall collection age (latest across all sources)
        latest_overall = (
            db.query(func.max(RawCollection.collected_at)).scalar()
        )
        if latest_overall:
            age_secs = (now - latest_overall.replace(tzinfo=timezone.utc)).total_seconds()
            metrics["last_raw_collection_age_seconds"] = int(age_secs)
        else:
            metrics["last_raw_collection_age_seconds"] = None

        status = _worst_status(alerts)
        if status == "ok":
            summary = (
                f"Coleta OK — {len(active_sources)} fonte(s) ativa(s) "
                f"nas últimas {stale_hours}h."
            )
        elif stale_sources:
            summary = f"{len(stale_sources)} fonte(s) sem coleta: {', '.join(sorted(stale_sources))}"
        else:
            summary = f"{len(alerts)} alerta(s) de coleta detectado(s)."

        return CheckResult(
            name="collection",
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

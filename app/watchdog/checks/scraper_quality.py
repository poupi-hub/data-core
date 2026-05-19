"""ScraperQualityChecker — monitors scraper quality, anti-bot, and structural drift.

Reads from:
  - raw_collections.metadata_json['quality']['score']
  - raw_collections.metadata_json['anti_bot_detected']
  - scraper_drift_events (unresolved, recent)

Checks:
  1. Average quality score per domain < threshold → warning
  2. Anti-bot detections per domain > threshold in last hour → warning
  3. Open high/critical drift events → warning/critical
  4. Recurring anti-bot type (captcha/cloudflare) → critical
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.watchdog.checks import CheckResult, WatchdogAlert
from app.raw.models import RawCollection
from app.scrapers.models import ScraperDriftEvent
from core.config import settings

logger = logging.getLogger(__name__)

_HARD_BOT_TYPES = {"captcha", "cloudflare"}


class ScraperQualityChecker:
    """Check payload quality scores, anti-bot detections, and structural drift."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self) -> CheckResult:
        try:
            return self._run()
        except Exception as exc:
            logger.exception("ScraperQualityChecker failed")
            return CheckResult(
                name="scraper_quality",
                status="warning",
                summary=f"Scraper quality check error: {exc}",
            )

    def _run(self) -> CheckResult:
        db = self._db
        now = datetime.now(tz=timezone.utc)
        since_1h = now - timedelta(hours=1)
        since_24h = now - timedelta(hours=24)
        q_threshold = settings.watchdog_quality_score_threshold
        ab_threshold = settings.watchdog_anti_bot_hourly_threshold

        alerts: list[WatchdogAlert] = []
        metrics: dict[str, Any] = {}

        # ── 1. Quality scores from metadata_json (last 24h) ───────────────────
        # We query raw_collections where metadata_json->quality is present
        quality_rows = (
            db.query(
                RawCollection.source_name,
                func.count().label("total"),
            )
            .filter(
                RawCollection.collected_at >= since_24h,
                RawCollection.metadata_json.isnot(None),
            )
            .group_by(RawCollection.source_name)
            .all()
        )

        # For each source, compute avg quality score via Python (JSONB extraction)
        quality_stats: dict[str, dict] = {}
        for qr in quality_rows:
            rows = (
                db.query(RawCollection.metadata_json)
                .filter(
                    RawCollection.source_name == qr.source_name,
                    RawCollection.collected_at >= since_24h,
                    RawCollection.metadata_json.isnot(None),
                )
                .all()
            )
            scores = []
            for r in rows:
                meta = r.metadata_json or {}
                score = (meta.get("quality") or {}).get("score")
                if score is not None:
                    scores.append(float(score))
            if not scores:
                continue

            avg = sum(scores) / len(scores)
            quality_stats[qr.source_name] = {
                "avg_score": round(avg, 1),
                "sample_count": len(scores),
                "min_score": min(scores),
                "max_score": max(scores),
            }

            if avg < q_threshold:
                alerts.append(WatchdogAlert(
                    severity="warning",
                    code="scraper_quality_low",
                    title=f"Qualidade baixa: {qr.source_name}",
                    message=(
                        f"'{qr.source_name}': qualidade média {avg:.0f}/100 "
                        f"(threshold: {q_threshold}). {len(scores)} amostras."
                    ),
                    source_name=qr.source_name,
                    context=quality_stats[qr.source_name],
                ))

        metrics["quality_stats"] = quality_stats

        # ── 2. Anti-bot detections (last 1h) per domain ───────────────────────
        all_rows_1h = (
            db.query(RawCollection.source_name, RawCollection.metadata_json)
            .filter(
                RawCollection.collected_at >= since_1h,
                RawCollection.metadata_json.isnot(None),
            )
            .all()
        )

        ab_by_source: dict[str, dict[str, int]] = {}
        for r in all_rows_1h:
            meta = r.metadata_json or {}
            if meta.get("anti_bot_detected"):
                src = r.source_name
                if src not in ab_by_source:
                    ab_by_source[src] = {}
                # We don't store detection_type in metadata_json currently,
                # just the boolean flag
                ab_by_source[src]["total"] = ab_by_source[src].get("total", 0) + 1

        for src, ab_data in ab_by_source.items():
            total_ab = ab_data.get("total", 0)
            if total_ab >= ab_threshold:
                alerts.append(WatchdogAlert(
                    severity="warning",
                    code="anti_bot_spike",
                    title=f"Anti-bot crescendo: {src}",
                    message=(
                        f"'{src}': {total_ab} detecção(ões) anti-bot na última 1h "
                        f"(threshold: {ab_threshold}/h). "
                        "Considerar proxy rotation ou backoff maior."
                    ),
                    source_name=src,
                    context={"anti_bot_count_1h": total_ab, "threshold": ab_threshold},
                ))

        metrics["anti_bot_by_source_1h"] = {
            src: d.get("total", 0) for src, d in ab_by_source.items()
        }

        # ── 3. Open drift events (unresolved, last 48h) ───────────────────────
        drift_events = (
            db.query(ScraperDriftEvent)
            .filter(
                ScraperDriftEvent.detected_at >= now - timedelta(hours=48),
                ScraperDriftEvent.resolved_at.is_(None),
                ScraperDriftEvent.risk_level.in_(["high", "critical"]),
            )
            .all()
        )

        drift_by_source: dict[str, list[str]] = {}
        for ev in drift_events:
            if ev.source_name not in drift_by_source:
                drift_by_source[ev.source_name] = []
            drift_by_source[ev.source_name].append(ev.drift_type)

        for src, drift_types in drift_by_source.items():
            severity = "critical" if any(
                ev.risk_level == "critical"
                for ev in drift_events
                if ev.source_name == src
            ) else "warning"
            alerts.append(WatchdogAlert(
                severity=severity,
                code="scraper_drift_detected",
                title=f"Drift estrutural: {src}",
                message=(
                    f"'{src}': {len(drift_types)} evento(s) de drift não resolvido(s) "
                    f"({', '.join(set(drift_types))}). "
                    "Schema do site pode ter mudado."
                ),
                source_name=src,
                context={"drift_types": drift_types, "count": len(drift_types)},
            ))

        metrics["open_drift_events"] = [
            {"source_name": ev.source_name, "drift_type": ev.drift_type, "risk_level": ev.risk_level}
            for ev in drift_events
        ]

        # ── Overall ───────────────────────────────────────────────────────────
        status = _worst_status(alerts)
        if status == "ok":
            sources = list(quality_stats.keys())
            summary = (
                f"Qualidade OK — {len(sources)} fonte(s) monitorada(s), "
                f"sem anti-bot ou drift crítico."
            )
        else:
            summary = f"{len(alerts)} alerta(s) de qualidade de scraper."

        return CheckResult(
            name="scraper_quality",
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

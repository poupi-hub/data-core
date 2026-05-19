"""WatchdogService — orchestrates all health checks, alerts, and Prometheus metrics.

Usage (from scheduler job)::

    with SessionLocal() as db:
        svc = WatchdogService(db)
        result = svc.run()          # watchdog check (30 min interval)
        svc.heartbeat()             # periodic Telegram summary (6h interval)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.watchdog.checks import CheckResult, WatchdogAlert
from app.watchdog.checks.collection import CollectionHealthChecker
from app.watchdog.checks.normalization import NormalizationHealthChecker
from app.watchdog.checks.scraper_quality import ScraperQualityChecker
from app.watchdog.checks.telegram_pub import TelegramPublicationChecker
from app.watchdog.heartbeat import HeartbeatFormatter, format_alert_message
from app.watchdog.models import WatchdogRun
from app.watchdog.notifier import TelegramNotifier
from api.metrics import (
    domains_with_active_alerts,
    last_normalized_offer_age_seconds,
    last_raw_collection_age_seconds,
    last_telegram_post_age_seconds,
    operational_watchdog_status,
    raw_to_normalized_success_rate,
    telegram_publish_failure_total,
    telegram_publish_success_total,
    watchdog_checks_total,
)
from core.config import settings

logger = logging.getLogger(__name__)

# Map status string to numeric value for the Prometheus gauge
_STATUS_VALUE = {"ok": 0, "warning": 1, "critical": 2}

# Codes where we send immediate Telegram even if Telegram is same alert as last run
# (avoid spamming same code repeatedly — tracked below)
_ALWAYS_ALERT_CODES = frozenset({
    "collection_platform_down",
    "telegram_publish_failing",
})


class WatchdogService:
    """Run all watchdog checks and manage alerting."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._notifier = TelegramNotifier()
        self._heartbeat_fmt = HeartbeatFormatter()

    # ── Public interface ──────────────────────────────────────────────────────

    def run(self) -> WatchdogRun:
        """Run all checks, send immediate alerts for critical issues, persist run record."""
        started = time.perf_counter()
        run_at = datetime.now(tz=timezone.utc)

        check_results: list[CheckResult] = []
        error_msg: str | None = None

        try:
            check_results = self._run_all_checks()
        except Exception as exc:
            logger.exception("WatchdogService.run() failed")
            error_msg = str(exc)

        duration_ms = int((time.perf_counter() - started) * 1000)

        all_alerts = [a for r in check_results for a in r.alerts]
        overall_status = self._overall_status(check_results)

        # ── Update Prometheus metrics ─────────────────────────────────────────
        self._update_metrics(check_results, all_alerts)

        # ── Send immediate Telegram alerts ────────────────────────────────────
        telegram_sent = False
        if all_alerts:
            telegram_sent = self._send_immediate_alerts(all_alerts)

        # ── Persist run record ────────────────────────────────────────────────
        run = WatchdogRun(
            run_at=run_at,
            overall_status=overall_status,
            duration_ms=duration_ms,
            check_results={r.name: r.to_dict() for r in check_results},
            alert_codes=[a.code for a in all_alerts],
            metrics_snapshot=self._build_metrics_snapshot(check_results),
            telegram_sent=telegram_sent,
            error_message=error_msg,
        )
        try:
            self._db.add(run)
            self._db.commit()
            self._db.refresh(run)
        except Exception:
            logger.exception("Failed to persist WatchdogRun")
            self._db.rollback()

        watchdog_checks_total.labels(status=overall_status).inc()

        logger.info(
            "Watchdog run complete",
            extra={
                "status": overall_status,
                "alerts": len(all_alerts),
                "duration_ms": duration_ms,
                "telegram_sent": telegram_sent,
            },
        )

        return run

    def heartbeat(self) -> bool:
        """Send periodic health summary to Telegram regardless of status."""
        try:
            check_results = self._run_all_checks()
        except Exception as exc:
            logger.exception("Watchdog heartbeat checks failed")
            check_results = []

        msg = self._heartbeat_fmt.format(
            check_results,
            heartbeat_interval_hours=settings.watchdog_heartbeat_hours,
        )
        sent = self._notifier.send(msg)
        logger.info(
            "Watchdog heartbeat sent",
            extra={"telegram_sent": sent, "check_count": len(check_results)},
        )
        return sent

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_all_checks(self) -> list[CheckResult]:
        db = self._db
        return [
            CollectionHealthChecker(db).run(),
            NormalizationHealthChecker(db).run(),
            ScraperQualityChecker(db).run(),
            TelegramPublicationChecker(db).run(),
        ]

    def _send_immediate_alerts(self, alerts: list[WatchdogAlert]) -> bool:
        """Send Telegram for critical alerts. Returns True if at least one sent."""
        criticals = [a for a in alerts if a.severity == "critical"]
        if not criticals:
            return False

        sent_any = False
        for alert in criticals:
            msg = format_alert_message(
                alert_code=alert.code,
                title=alert.title,
                message=alert.message,
                severity=alert.severity,
            )
            ok = self._notifier.send(msg)
            if ok:
                sent_any = True
                telegram_publish_success_total.inc()
            else:
                telegram_publish_failure_total.inc()

        return sent_any

    def _update_metrics(
        self, check_results: list[CheckResult], all_alerts: list[WatchdogAlert]
    ) -> None:
        """Update Prometheus gauges from check results."""
        try:
            # Per-check status gauge
            for result in check_results:
                operational_watchdog_status.labels(check=result.name).set(
                    _STATUS_VALUE.get(result.status, 1)
                )

            # Collection age
            for result in check_results:
                if result.name == "collection":
                    age = result.metrics.get("last_raw_collection_age_seconds")
                    if age is not None:
                        last_raw_collection_age_seconds.set(age)

                elif result.name == "normalization":
                    age = result.metrics.get("last_normalized_age_seconds")
                    if age is not None:
                        last_normalized_offer_age_seconds.set(age)

                    # Raw→normalized success rate (average across sources)
                    src_rates = result.metrics.get("source_rates", {})
                    if src_rates:
                        rates = [v["success_rate"] for v in src_rates.values()]
                        avg_rate = sum(rates) / len(rates)
                        raw_to_normalized_success_rate.set(avg_rate)

                elif result.name == "telegram":
                    age = result.metrics.get("last_telegram_post_age_seconds")
                    if age is not None:
                        last_telegram_post_age_seconds.set(age)

            # Domains with active alerts
            alerting_sources = {
                a.source_name for a in all_alerts if a.source_name
            }
            domains_with_active_alerts.set(len(alerting_sources))

        except Exception:
            logger.exception("Failed to update watchdog Prometheus metrics")

    def _overall_status(self, check_results: list[CheckResult]) -> str:
        statuses = {r.status for r in check_results}
        if "critical" in statuses:
            return "critical"
        if "warning" in statuses:
            return "warning"
        return "ok"

    def _build_metrics_snapshot(self, check_results: list[CheckResult]) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        for r in check_results:
            snapshot[r.name] = r.metrics
        return snapshot

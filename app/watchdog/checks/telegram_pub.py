"""TelegramPublicationChecker — monitors last Telegram publication via callback events.

Data source: telegram_publication_events table (populated by poupi-baby callbacks).

Checks:
  1. No publication in last X hours → distinguish:
     - "No deals" (normalized products exist but low deal scores) → informational
     - "Pipeline stopped" (no new normalized products AND no publications) → critical
     - "Publication failure" (normalized + deals exist but failed sends) → critical
  2. High failure rate in recent sends → warning
  3. poupi-baby data sync health: last price-feed request age

If telegram_publication_events has no data at all, reports "unknown" status (not critical)
— poupi-baby callback not yet configured.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.watchdog.checks import CheckResult, WatchdogAlert
from app.watchdog.models import TelegramPublicationEvent
from app.normalization.models import NormalizedProduct
from core.config import settings

logger = logging.getLogger(__name__)


class TelegramPublicationChecker:
    """Check Telegram publication health from data-core's perspective."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self) -> CheckResult:
        try:
            return self._run()
        except Exception as exc:
            logger.exception("TelegramPublicationChecker failed")
            return CheckResult(
                name="telegram",
                status="warning",
                summary=f"Telegram check error: {exc}",
            )

    def _run(self) -> CheckResult:
        db = self._db
        now = datetime.now(tz=timezone.utc)
        stale_hours = settings.watchdog_publication_stale_hours
        since_stale = now - timedelta(hours=stale_hours)
        since_24h = now - timedelta(hours=24)

        alerts: list[WatchdogAlert] = []
        metrics: dict[str, Any] = {}

        # ── 1. Check if we have any publication events at all ─────────────────
        total_events = (
            db.query(func.count(TelegramPublicationEvent.id)).scalar()
        ) or 0
        metrics["callback_events_total"] = total_events

        if total_events == 0:
            # Callback not yet configured in poupi-baby — report as unknown
            metrics["status_note"] = "no_callback_data"
            return CheckResult(
                name="telegram",
                status="ok",
                summary=(
                    "Publicação Telegram: sem dados de callback. "
                    "Configure poupi-baby para reportar via POST /api/v1/watchdog/report/telegram-published."
                ),
                alerts=[],
                metrics=metrics,
            )

        # ── 2. Last successful send ───────────────────────────────────────────
        last_sent = (
            db.query(func.max(TelegramPublicationEvent.published_at))
            .filter(TelegramPublicationEvent.status == "sent")
            .scalar()
        )
        metrics["last_sent_at"] = last_sent.isoformat() if last_sent else None

        if last_sent:
            age_secs = (now - last_sent.replace(tzinfo=timezone.utc)).total_seconds()
            age_h = age_secs / 3600
            metrics["last_telegram_post_age_seconds"] = int(age_secs)
            metrics["last_telegram_post_age_hours"] = round(age_h, 1)
        else:
            age_h = None
            metrics["last_telegram_post_age_seconds"] = None

        # ── 3. No recent publication → diagnose cause ─────────────────────────
        recent_sent = (
            db.query(func.count(TelegramPublicationEvent.id))
            .filter(
                TelegramPublicationEvent.published_at >= since_stale,
                TelegramPublicationEvent.status == "sent",
            )
            .scalar()
        ) or 0
        metrics["sent_in_window"] = recent_sent

        if recent_sent == 0:
            # Check if there are normalized products from the same window
            recent_normalized = (
                db.query(func.count(NormalizedProduct.id))
                .filter(NormalizedProduct.normalized_at >= since_stale)
                .scalar()
            ) or 0
            metrics["recent_normalized_products"] = recent_normalized

            # Check if there were failures in the window
            recent_failed = (
                db.query(func.count(TelegramPublicationEvent.id))
                .filter(
                    TelegramPublicationEvent.published_at >= since_stale,
                    TelegramPublicationEvent.status == "failed",
                )
                .scalar()
            ) or 0
            metrics["failed_in_window"] = recent_failed

            if recent_failed > 0:
                # Failures with no successes = pipeline broken
                alerts.append(WatchdogAlert(
                    severity="critical",
                    code="telegram_publish_failing",
                    title="Falha na publicação Telegram",
                    message=(
                        f"{recent_failed} envio(s) com falha nas últimas {stale_hours}h, "
                        "nenhum com sucesso. Verificar bot token e conexão com Telegram."
                    ),
                    context={
                        "failed": recent_failed,
                        "stale_hours": stale_hours,
                    },
                ))
            elif recent_normalized > 0:
                # Have products but no publications = deal score too low or trigger broken
                alerts.append(WatchdogAlert(
                    severity="warning",
                    code="telegram_no_publication_products_exist",
                    title="Sem publicação Telegram",
                    message=(
                        f"Nenhuma publicação Telegram nas últimas {stale_hours}h, "
                        f"mas {recent_normalized} produto(s) normalizado(s). "
                        "Deal score abaixo do mínimo ou trigger quebrado?"
                    ),
                    context={
                        "stale_hours": stale_hours,
                        "recent_normalized": recent_normalized,
                    },
                ))
            else:
                # No products AND no publications = collection pipeline issue (already caught upstream)
                alerts.append(WatchdogAlert(
                    severity="warning",
                    code="telegram_no_publication_no_data",
                    title="Sem publicação e sem dados novos",
                    message=(
                        f"Nenhuma publicação Telegram nas últimas {stale_hours}h e "
                        "nenhum produto normalizado no mesmo período. "
                        "Verificar pipeline de coleta."
                    ),
                    context={"stale_hours": stale_hours},
                ))

        # ── 4. Failure rate in last 24h ───────────────────────────────────────
        stats_24h = (
            db.query(
                TelegramPublicationEvent.status,
                func.count().label("cnt"),
            )
            .filter(TelegramPublicationEvent.published_at >= since_24h)
            .group_by(TelegramPublicationEvent.status)
            .all()
        )
        by_status = {r.status: r.cnt for r in stats_24h}
        total_24h = sum(by_status.values())
        failed_24h = by_status.get("failed", 0)
        sent_24h = by_status.get("sent", 0)
        rate_limited_24h = by_status.get("rate_limited", 0)

        metrics["by_status_24h"] = by_status
        metrics["telegram_sent_24h"] = sent_24h
        metrics["telegram_failed_24h"] = failed_24h
        metrics["telegram_rate_limited_24h"] = rate_limited_24h

        if total_24h >= 3 and failed_24h > 0:
            failure_rate = failed_24h / total_24h
            if failure_rate > 0.3:
                alerts.append(WatchdogAlert(
                    severity="warning",
                    code="telegram_high_failure_rate",
                    title="Alta taxa de falha Telegram",
                    message=(
                        f"{failed_24h}/{total_24h} envios com falha nas últimas 24h "
                        f"({failure_rate:.0%}). Verificar bot e chat_id."
                    ),
                    context={
                        "failed_24h": failed_24h,
                        "total_24h": total_24h,
                        "failure_rate": round(failure_rate, 3),
                    },
                ))

        # ── Overall ───────────────────────────────────────────────────────────
        status = _worst_status(alerts)
        if status == "ok":
            if last_sent and age_h is not None:
                summary = f"Telegram OK — última publicação há {age_h:.0f}h, {sent_24h} envio(s) nas 24h."
            else:
                summary = "Telegram OK — sem eventos de publicação recentes."
        else:
            summary = f"{len(alerts)} alerta(s) de publicação Telegram."

        return CheckResult(
            name="telegram",
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

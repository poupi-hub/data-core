"""Main orchestrator: gather data, format, and send Telegram summaries.

TelegramSummaryService coordinates three summary types and the alert check:
  • send_operational_summary(db) — hourly operational health
  • send_quant_summary(db)       — 6h quant/adaptive intelligence
  • send_longitudinal_summary(db)— daily 24h vs 7d digest
  • check_and_send_alerts(db)    — immediate alerts (with cooldown)

All methods return bool (True = sent successfully).
All exceptions are caught, logged, and counted as failures — never re-raised.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.telegram_summary import channel_resolver as _cr
from app.telegram_summary import metrics as _m
from app.telegram_summary.formatters.alert_formatter import format_alert
from app.telegram_summary.formatters.longitudinal_formatter import format_longitudinal_summary
from app.telegram_summary.formatters.operational_formatter import format_operational_summary
from app.telegram_summary.formatters.quant_formatter import format_quant_summary
from app.telegram_summary.services.alert_service import get_alert_service
from app.telegram_summary.services.longitudinal_summary_service import LongitudinalSummaryService
from app.telegram_summary.services.operational_summary_service import OperationalSummaryService
from app.telegram_summary.services.quant_summary_service import QuantSummaryService
from app.watchdog.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class TelegramSummaryService:
    """Orchestrate data gathering, formatting, and Telegram message delivery."""

    def __init__(self) -> None:
        self._notifier = TelegramNotifier()

    def _notifier_for(self, event_type: str) -> TelegramNotifier:
        """Return a TelegramNotifier targeting the resolved channel for event_type."""
        chat_id = _cr.resolve_chat_id(event_type)
        return TelegramNotifier(chat_id=chat_id)

    # ── Public API ─────────────────────────────────────────────────────────────

    def send_operational_summary(self, db: Session) -> bool:
        """Gather operational health data and send the hourly summary."""
        return self._send(
            summary_type="operational",
            gather_fn=lambda: OperationalSummaryService(db).gather(),
            format_fn=format_operational_summary,
        )

    def send_quant_summary(self, db: Session) -> bool:
        """Gather quant metrics and send the 6h summary."""
        return self._send(
            summary_type="quant",
            gather_fn=lambda: QuantSummaryService(db).gather(),
            format_fn=format_quant_summary,
        )

    def send_longitudinal_summary(self, db: Session) -> bool:
        """Gather 24h vs 7d comparison data and send the daily digest."""
        return self._send(
            summary_type="longitudinal",
            gather_fn=lambda: LongitudinalSummaryService(db).gather(),
            format_fn=format_longitudinal_summary,
        )


    def check_and_send_alerts(self, db: Session) -> int:
        """Evaluate alert conditions and send any that are active and off-cooldown.

        Returns the number of alerts successfully sent.
        """
        sent = 0
        try:
            operational = OperationalSummaryService(db).gather()
        except Exception:
            logger.exception("telegram_summary: alert check — operational gather failed")
            return 0

        alert_svc = get_alert_service()
        pending = alert_svc.evaluate(operational)

        for alert in pending:
            try:
                label = _cr.resolve_label(alert.alert_type)
                html = label + format_alert(alert)
                notifier = self._notifier_for(alert.alert_type)
                ok = notifier.send(html)
                if ok:
                    alert_svc.mark_sent(alert.alert_type)
                    _m.telegram_alert_sent_total.labels(
                        alert_type=alert.alert_type,
                        severity=alert.severity,
                    ).inc()
                    sent += 1
                    logger.info(
                        "telegram_summary: alert sent",
                        extra={"alert_type": alert.alert_type, "severity": alert.severity},
                    )
                else:
                    _m.telegram_summary_failures_total.labels(summary_type="alert").inc()
            except Exception:
                logger.exception(
                    "telegram_summary: alert send failed — %s", alert.alert_type
                )
                _m.telegram_summary_failures_total.labels(summary_type="alert").inc()

        return sent

    # ── Internal ───────────────────────────────────────────────────────────────

    def _send(self, *, summary_type: str, gather_fn, format_fn) -> bool:
        try:
            payload = gather_fn()
            label = _cr.resolve_label(summary_type)
            html = label + format_fn(payload)
            notifier = self._notifier_for(summary_type)
            ok = notifier.send(html)
            if ok:
                _m.telegram_summary_sent_total.labels(summary_type=summary_type).inc()
                logger.info(
                    "telegram_summary: summary sent",
                    extra={"summary_type": summary_type},
                )
            else:
                _m.telegram_summary_failures_total.labels(summary_type=summary_type).inc()
            return ok
        except Exception:
            logger.exception("telegram_summary: %s send failed", summary_type)
            _m.telegram_summary_failures_total.labels(summary_type=summary_type).inc()
            return False

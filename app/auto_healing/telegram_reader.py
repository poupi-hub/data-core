from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.auto_healing.models import OperationalAlert
from app.watchdog.models import WatchdogRun
from core.config import settings


class TelegramAlertReader:
    """Reads operational alerts from data-core's persisted outgoing alert history.

    Telegram Bot API is not a reliable source for reading group history, so this
    uses WatchdogRun records created before Telegram sends. If that table has no
    data, the reader returns an empty list and future outgoing alerts remain
    covered by the existing watchdog persistence.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def recent_alerts(self) -> list[OperationalAlert]:
        since = datetime.now(timezone.utc) - timedelta(hours=settings.auto_healing_alert_window_hours)
        rows = (
            self._db.query(WatchdogRun)
            .filter(WatchdogRun.run_at >= since)
            .order_by(WatchdogRun.run_at.desc())
            .limit(50)
            .all()
        )

        alerts: list[OperationalAlert] = []
        for row in rows:
            for item in self._extract_alerts(row):
                alerts.append(item)
        return alerts

    def _extract_alerts(self, run: WatchdogRun) -> list[OperationalAlert]:
        results = run.check_results or {}
        extracted: list[OperationalAlert] = []
        if isinstance(results, dict):
            for check_name, check_result in results.items():
                if not isinstance(check_result, dict):
                    continue
                for alert in check_result.get("alerts") or []:
                    if not isinstance(alert, dict):
                        continue
                    extracted.append(_alert_from_payload(alert, check_name, run.run_at))

        if extracted:
            return extracted

        # Older/partial rows may only have alert_codes. Keep them analyzable.
        for code in run.alert_codes or []:
            extracted.append(
                OperationalAlert(
                    code=str(code),
                    title=str(code),
                    message="Alert persisted without expanded payload.",
                    severity="warning" if run.overall_status != "critical" else "critical",
                    emitted_at=run.run_at,
                    evidence={"watchdog_run_id": run.id, "overall_status": run.overall_status},
                )
            )
        return extracted


def _alert_from_payload(payload: dict[str, Any], check_name: str, run_at: datetime) -> OperationalAlert:
    return OperationalAlert(
        code=str(payload.get("code") or "unknown_alert"),
        title=str(payload.get("title") or payload.get("code") or "Operational alert"),
        message=str(payload.get("message") or ""),
        severity=str(payload.get("severity") or "warning"),
        source=payload.get("source_name") or check_name,
        emitted_at=run_at,
        evidence=dict(payload.get("context") or {}),
    )


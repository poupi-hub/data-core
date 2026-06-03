from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.auto_healing.health_checker import HealthChecker
from app.auto_healing.incident_classifier import IncidentClassifier
from app.auto_healing.models import GeneralStatus, WatchdogExecution
from app.auto_healing.reporter import AutoHealingReporter
from app.auto_healing.safe_fixes import SafeFixEngine
from app.auto_healing.telegram_reader import TelegramAlertReader
from core.config import settings

logger = logging.getLogger(__name__)


class AutoHealingWatchdog:
    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self) -> WatchdogExecution:
        started = time.perf_counter()
        errors: list[str] = []
        timestamp = datetime.now(timezone.utc)

        try:
            alerts = TelegramAlertReader(self._db).recent_alerts()
        except Exception as exc:
            logger.exception("auto_healing: failed reading alerts")
            alerts = []
            errors.append(f"telegram_reader: {exc}")

        try:
            health = HealthChecker(self._db).run()
        except Exception as exc:
            logger.exception("auto_healing: failed health checks")
            health = []
            errors.append(f"health_checker: {exc}")

        assessments = IncidentClassifier().classify(alerts, health)
        actions, manual = SafeFixEngine(self._db, dry_run=settings.auto_healing_dry_run).apply(assessments, health)
        status = _general_status(health, assessments, errors)

        execution = WatchdogExecution(
            timestamp=timestamp,
            status=status,
            dry_run=settings.auto_healing_dry_run,
            alerts_analyzed=assessments,
            service_health=health,
            actions=actions,
            manual_pending=manual,
            errors=errors,
        )
        _append_history(execution)

        try:
            AutoHealingReporter().send(execution)
        except Exception as exc:
            logger.warning("auto_healing: telegram report failed: %s", exc)

        logger.info(
            "Auto-Healing Watchdog finished",
            extra={
                "status": status.value,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "alerts": len(assessments),
                "actions": len(actions),
                "dry_run": settings.auto_healing_dry_run,
            },
        )
        return execution


def _general_status(health, assessments, errors: list[str]) -> GeneralStatus:
    if errors or any(item.critical for item in health):
        return GeneralStatus.CRITICAL
    if any(not item.ok for item in health):
        return GeneralStatus.DEGRADED
    if any(item.classification.value == "REAL" for item in assessments):
        return GeneralStatus.DEGRADED
    return GeneralStatus.HEALTHY


def _append_history(execution: WatchdogExecution) -> None:
    path = Path(settings.auto_healing_history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_history_if_needed(path, _history_max_bytes())
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(execution.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def _history_max_bytes() -> int:
    max_mb = settings.auto_healing_history_max_mb
    if max_mb <= 0:
        max_mb = 10
    return max_mb * 1024 * 1024


def _rotate_history_if_needed(path: Path, max_bytes: int) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        rotated = path.with_name(f"{path.name}.{suffix}")
        path.replace(rotated)
    except Exception as exc:
        # Rotation must never risk losing the current watchdog event. If rotation
        # fails, append to the existing file and surface the issue in logs.
        logger.warning("auto_healing: history rotation failed: %s", exc)

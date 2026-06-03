from __future__ import annotations

import logging

from core.config import settings
from database.session import SessionLocal

logger = logging.getLogger(__name__)
MIN_AUTO_HEALING_INTERVAL_MINUTES = 15


def auto_healing_watchdog_job() -> None:
    if not settings.auto_healing_enabled:
        logger.debug("auto_healing_watchdog_job: disabled via AUTO_HEALING_ENABLED")
        return

    from app.auto_healing.watchdog import AutoHealingWatchdog

    db = SessionLocal()
    try:
        AutoHealingWatchdog(db).run()
    finally:
        db.close()


def effective_auto_healing_interval_minutes() -> int:
    try:
        configured = int(settings.auto_healing_interval_minutes)
    except (TypeError, ValueError):
        logger.warning(
            "auto_healing: invalid interval clamped to safe minimum",
            extra={"minimum_minutes": MIN_AUTO_HEALING_INTERVAL_MINUTES},
        )
        return MIN_AUTO_HEALING_INTERVAL_MINUTES
    if configured < MIN_AUTO_HEALING_INTERVAL_MINUTES:
        logger.warning(
            "auto_healing: interval clamped to safe minimum",
            extra={
                "configured_minutes": configured,
                "minimum_minutes": MIN_AUTO_HEALING_INTERVAL_MINUTES,
            },
        )
        return MIN_AUTO_HEALING_INTERVAL_MINUTES
    return configured

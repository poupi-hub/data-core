"""Auto-Healing Watchdog -- Detect -> Heal -> Verify -> Notify.

Cycle:
1. Detect  -- run all health checks via HealthChecker
2. Heal    -- attempt auto-healing for each unhealthy service
3. Verify  -- re-run health checks for services that were healed
4. Notify  -- send Telegram only for FAILED or SKIPPED critical outcomes
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.auto_healing.cooldown import CircuitBreaker, CooldownManager
from app.auto_healing.healer import find_healer, run_healers
from app.auto_healing.health_checker import HealthChecker
from app.auto_healing.incident_classifier import IncidentClassifier
from app.auto_healing.models import (
    GeneralStatus,
    HealOutcome,
    HealResult,
    ServiceHealth,
    WatchdogExecution,
)
from app.auto_healing.telegram_reader import TelegramAlertReader
from core.config import settings

logger = logging.getLogger(__name__)

_VERIFY_WAIT_SECONDS = 30  # container restarts need time to write heartbeat


class AutoHealingWatchdog:
    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self) -> WatchdogExecution:
        started = time.perf_counter()
        errors: list[str] = []
        timestamp = datetime.now(timezone.utc)

        # 1. READ RECENT ALERTS
        try:
            alerts = TelegramAlertReader(self._db).recent_alerts()
        except Exception as exc:
            logger.exception("auto_healing: failed reading alerts")
            alerts = []
            errors.append(f"telegram_reader: {exc}")

        # 2. DETECT
        try:
            health = HealthChecker(self._db).run()
        except Exception as exc:
            logger.exception("auto_healing: failed health checks")
            health = []
            errors.append(f"health_checker: {exc}")

        # 3. HEAL (with cooldown + circuit breaker guard)
        heal_results: list[HealResult] = []
        if not settings.auto_healing_dry_run and any(not item.ok for item in health):
            try:
                heal_results = _run_healers_guarded(health, self._db)
            except Exception as exc:
                logger.exception("auto_healing: healer raised")
                errors.append(f"healer: {exc}")

        # 4. VERIFY
        healed_services = {r.service for r in heal_results if r.outcome != HealOutcome.SKIPPED}
        if healed_services:
            time.sleep(_VERIFY_WAIT_SECONDS)
            try:
                verified = HealthChecker(self._db).run()
            except Exception as exc:
                logger.exception("auto_healing: verify health check failed")
                verified = health
                errors.append(f"verify_checker: {exc}")
            heal_results = _reconcile_outcomes(heal_results, verified)
        else:
            verified = health

        # 5. CLASSIFY (for history / metrics)
        events = IncidentClassifier().classify(alerts, verified, now=timestamp)
        status = _general_status(verified, events, errors)

        # 6. NOTIFY -- only on FAILED / SKIPPED critical outcomes
        _notify(heal_results, verified, errors)

        execution = WatchdogExecution(
            timestamp=timestamp,
            status=status,
            dry_run=settings.auto_healing_dry_run,
            events=events,
            service_health=verified,
            heal_results=heal_results,
            errors=errors,
        )
        _append_history(execution)

        logger.info(
            "Auto-Healing Watchdog finished",
            extra={
                "status": status.value,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "events": len(events),
                "healed": len([r for r in heal_results if r.outcome == HealOutcome.RECOVERED]),
                "failed": len([r for r in heal_results if r.outcome == HealOutcome.FAILED]),
                "skipped": len([r for r in heal_results if r.outcome == HealOutcome.SKIPPED]),
                "dry_run": settings.auto_healing_dry_run,
            },
        )
        return execution


_cooldown = CooldownManager()
_circuit = CircuitBreaker()


def _run_healers_guarded(
    health_list: list[ServiceHealth],
    db: Session,
) -> list[HealResult]:
    """Run healers with cooldown and circuit breaker gates.

    For each unhealthy service:
    1. Check circuit breaker → BLOCKED_CIRCUIT if open
    2. Check cooldown budget → SKIPPED if exhausted
    3. Run healer
    4. Record outcome for cooldown / circuit state
    """
    results: list[HealResult] = []
    for item in health_list:
        if item.ok:
            continue
        healer = find_healer(item)
        if healer is None:
            if item.critical:
                results.append(
                    HealResult(
                        service=item.name,
                        outcome=HealOutcome.SKIPPED,
                        detail="no healer registered for this service",
                    )
                )
            continue

        # Circuit breaker check
        circuit_status = _circuit.status(item.name)
        if circuit_status.open:
            logger.warning(
                "watchdog: circuit OPEN for service=%s — blocking heal. %s",
                item.name, circuit_status.reason,
            )
            results.append(
                HealResult(
                    service=item.name,
                    outcome=HealOutcome.BLOCKED_CIRCUIT,
                    detail=f"Circuit breaker open: {circuit_status.reason}",
                )
            )
            continue

        # Cooldown check
        cooldown_status = _cooldown.check(item.name)
        if not cooldown_status.allowed:
            logger.warning(
                "watchdog: cooldown active for service=%s — skipping heal. %s",
                item.name, cooldown_status.reason,
            )
            results.append(
                HealResult(
                    service=item.name,
                    outcome=HealOutcome.SKIPPED,
                    detail=f"Cooldown: {cooldown_status.reason}",
                )
            )
            continue

        # Run healer
        logger.info("watchdog: running healer=%s for service=%s", healer.name, item.name)
        _cooldown.record_restart(item.name)
        result = healer.heal(db)
        results.append(result)

        # Update circuit state
        if result.outcome == HealOutcome.FAILED:
            circuit_after = _circuit.record_failure(item.name)
            if circuit_after.open:
                logger.warning(
                    "watchdog: circuit OPENED for service=%s after %d consecutive failures",
                    item.name, circuit_after.consecutive_failures,
                )
        elif result.outcome == HealOutcome.RECOVERED:
            _circuit.record_success(item.name)

    return results


def _notify(
    heal_results: list[HealResult],
    verified: list[ServiceHealth],
    errors: list[str],
) -> None:
    if not heal_results:
        return
    from app.auto_healing.notifier import build_notifier

    notifier = build_notifier()
    if notifier is None:
        return
    health_map = {item.name: item for item in verified}
    try:
        notifier.notify(heal_results, health_map)
    except Exception as exc:
        logger.warning("auto_healing: notifier failed: %s", exc)
        errors.append(f"notifier: {exc}")


def _reconcile_outcomes(
    results: list[HealResult], verified: list[ServiceHealth]
) -> list[HealResult]:
    health_map = {item.name: item for item in verified}
    reconciled: list[HealResult] = []
    for result in results:
        if result.outcome != HealOutcome.RECOVERED:
            reconciled.append(result)
            continue
        item = health_map.get(result.service)
        if item is None or item.ok:
            reconciled.append(result)
        else:
            reconciled.append(
                HealResult(
                    service=result.service,
                    outcome=HealOutcome.FAILED,
                    detail=f"{result.detail} -- service still unhealthy after heal",
                    rows_affected=result.rows_affected,
                    error=result.error,
                )
            )
    return reconciled


def _general_status(
    health: list[ServiceHealth],
    events: list,
    errors: list[str],
) -> GeneralStatus:
    if errors or any(item.critical for item in health):
        return GeneralStatus.CRITICAL
    if any(not item.ok for item in health):
        return GeneralStatus.DEGRADED
    if any(item.classification.value in {"DEGRADED", "AUTO_HEALABLE_DRY_RUN"} for item in events):
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
        logger.warning("auto_healing: history rotation failed: %s", exc)
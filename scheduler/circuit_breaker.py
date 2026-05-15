"""Source-level circuit breaker.

Checks recent CollectionRun history for a (module, source_name) pair.
When the last N consecutive runs all failed, auto-deactivates targets
and writes a CollectorError dead-letter so operators can investigate.

Usage:
    from scheduler.circuit_breaker import check_source_circuit

    check_source_circuit(db, module="ecommerce", source_name="drogasil")
"""

import logging
from datetime import datetime, timezone

from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus
from database.session import SessionLocal
from notifications.webhook import send_webhook
from api.metrics import circuit_breaker_opens_total

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURES_THRESHOLD = 5   # open circuit after this many consecutive failed runs
CIRCUIT_OPEN_ERROR_TYPE       = "CircuitOpen"
CIRCUIT_REOPEN_ERROR_TYPE     = "CircuitAutoReopened"


def check_source_circuit(
    db,
    *,
    module: str,
    source_name: str,
    threshold: int = CONSECUTIVE_FAILURES_THRESHOLD,
) -> bool:
    """Return True if the circuit was just opened (targets deactivated), False otherwise."""
    recent_runs = (
        db.query(CollectionRun)
        .filter(
            CollectionRun.module == module,
            CollectionRun.source_name == source_name,
            CollectionRun.status.in_([RunStatus.success, RunStatus.failed]),
        )
        .order_by(CollectionRun.finished_at.desc().nullslast())
        .limit(threshold)
        .all()
    )

    if len(recent_runs) < threshold:
        return False  # not enough history to decide

    all_failed = all(r.status == RunStatus.failed for r in recent_runs)
    if not all_failed:
        return False

    # Check if circuit is already open (avoid spamming errors)
    already_open = (
        db.query(CollectorError)
        .filter(
            CollectorError.collector_name == source_name,
            CollectorError.error_type == CIRCUIT_OPEN_ERROR_TYPE,
            CollectorError.resolved_at.is_(None),
        )
        .first()
    )
    if already_open:
        return False  # already open, nothing to do

    # Open the circuit: deactivate all active targets for this source
    targets = (
        db.query(CollectionTarget)
        .filter(
            CollectionTarget.module == module,
            CollectionTarget.source_name == source_name,
            CollectionTarget.active.is_(True),
        )
        .all()
    )

    for target in targets:
        target.active = False

    # Write a dead-letter error so operators can see it and resolve
    db.add(
        CollectorError(
            collector_name=source_name,
            error_type=CIRCUIT_OPEN_ERROR_TYPE,
            message=(
                f"Circuit opened: {len(recent_runs)} consecutive failed runs for "
                f"module={module} source={source_name}. "
                f"{len(targets)} target(s) deactivated."
            ),
            context={
                "module": module,
                "source_name": source_name,
                "failed_run_ids": [str(r.id) for r in recent_runs],
                "deactivated_targets": len(targets),
                "threshold": threshold,
            },
        )
    )

    db.commit()

    logger.warning(
        "Circuit opened for source",
        extra={
            "pipeline_module": module,
            "source_name": source_name,
            "consecutive_failures": len(recent_runs),
            "deactivated_targets": len(targets),
        },
    )

    circuit_breaker_opens_total.labels(module=module, source_name=source_name).inc()

    send_webhook({
        "source": "data-core",
        "event": "circuit_opened",
        "module": module,
        "source_name": source_name,
        "consecutive_failures": len(recent_runs),
        "deactivated_targets": len(targets),
        "failed_run_ids": [str(r.id) for r in recent_runs],
    })

    return True


def reopen_source_circuit(db, *, module: str, source_name: str) -> int:
    """Re-activate targets for a source and resolve open circuit errors.

    Called manually (e.g. via admin API) when the underlying issue is fixed.
    Returns number of targets reactivated.
    """
    targets = (
        db.query(CollectionTarget)
        .filter(
            CollectionTarget.module == module,
            CollectionTarget.source_name == source_name,
            CollectionTarget.active.is_(False),
        )
        .all()
    )

    for target in targets:
        target.active = True

    # Resolve open circuit errors
    open_errors = (
        db.query(CollectorError)
        .filter(
            CollectorError.collector_name == source_name,
            CollectorError.error_type == CIRCUIT_OPEN_ERROR_TYPE,
            CollectorError.resolved_at.is_(None),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for err in open_errors:
        err.resolved_at = now
        err.resolution_note = "Circuit manually reopened by operator"

    db.add(
        CollectorError(
            collector_name=source_name,
            error_type=CIRCUIT_REOPEN_ERROR_TYPE,
            message=f"Circuit reopened: {len(targets)} target(s) reactivated for module={module} source={source_name}.",
            context={"module": module, "source_name": source_name, "reactivated_targets": len(targets)},
        )
    )

    db.commit()
    logger.info(
        "Circuit reopened for source",
        extra={"pipeline_module": module, "source_name": source_name, "reactivated": len(targets)},
    )
    return len(targets)

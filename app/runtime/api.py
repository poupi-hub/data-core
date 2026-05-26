"""Runtime diagnosis endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.runtime.scheduler_reliability import (
    MODE_ORDER,
    PRIORITY_VALUE,
    SchedulerReliabilityEngine,
    scheduler_reliability_audit_report,
)
from app.runtime.scheduler_watchdog import DataCoreSchedulerWatchdog, format_scheduler_alert_payload
from database.session import get_db

router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])


@router.get("/scheduler-diagnosis", summary="Data-core scheduler runtime diagnosis")
def scheduler_diagnosis(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return read-only preventive memory diagnosis for the scheduler container."""
    return DataCoreSchedulerWatchdog().diagnose(db).to_dict()


@router.get("/scheduler-summary", summary="Short data-core scheduler operational summary")
def scheduler_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return a short scheduler summary suitable for Telegram and ops views."""
    diagnosis = DataCoreSchedulerWatchdog().diagnose(db)
    return diagnosis.to_summary()


@router.get("/scheduler-alert-payload", summary="Preview scheduler Telegram alert payload")
def scheduler_alert_payload(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the current Telegram payload without sending any message."""
    diagnosis = DataCoreSchedulerWatchdog().diagnose(db)
    return format_scheduler_alert_payload(diagnosis)


@router.get("/scheduler-protection", summary="Adaptive scheduler reliability protection state")
def scheduler_protection(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the current adaptive protection decision without applying changes."""
    return SchedulerReliabilityEngine().decide("runtime_summary", priority="HIGH", db=db).to_dict()


@router.get(
    "/scheduler-reliability-audit",
    summary="Scheduler reliability dry-run audit calibration report",
)
def scheduler_reliability_audit(
    last_minutes: int | None = Query(default=None, ge=1, le=10080),
    mode: str | None = Query(
        default=None,
        pattern="^(NORMAL|CONSERVATIVE|PROTECTIVE|CRITICAL_PROTECTION)$",
    ),
    job_priority: str | None = Query(default=None, pattern="^(CRITICAL|HIGH|NORMAL|LOW)$"),
) -> dict[str, Any]:
    """Return aggregated dry-run decisions and recent audit events without applying controls."""
    return scheduler_reliability_audit_report(
        last_minutes=last_minutes,
        mode=mode if mode in MODE_ORDER else None,
        job_priority=job_priority if job_priority in PRIORITY_VALUE else None,
    )

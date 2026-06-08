"""Auto-healing actions — Detect → Heal → Verify → Notify.

Each healer targets a specific service failure and attempts a safe, reversible
repair. Healers must be idempotent: running twice on an already-healthy service
must produce no harmful side effects.

Heal safety rules:
- DB operations use explicit transactions; rollback on error.
- Redis operations are best-effort; failures degrade gracefully.
- No healer may restart processes, deploy code, or modify configuration.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auto_healing.models import HealOutcome, HealResult, ServiceHealth

logger = logging.getLogger(__name__)

# Maximum records to reset per heal cycle — prevents runaway bulk updates.
_NORM_RESET_LIMIT = 500
_BULLMQ_STALLED_LIMIT = 100


@runtime_checkable
class Healer(Protocol):
    name: str

    def can_heal(self, health: ServiceHealth) -> bool: ...

    def heal(self, db: Session) -> HealResult: ...


class NormalizationBacklogHealer:
    """Reset normalization_failed records to normalization_pending for retry.

    Safe: the normalization pipeline is idempotent — re-processing a record
    that already succeeded produces a duplicate that the deduplication layer
    discards.
    """

    name = "reset_normalization_failed"

    def can_heal(self, health: ServiceHealth) -> bool:
        if health.name != "queues":
            return False
        failed = health.evidence.get("failed_normalization", 0)
        return int(failed) > 0

    def heal(self, db: Session) -> HealResult:
        try:
            result = db.execute(
                text(
                    "UPDATE raw_collections"
                    " SET processing_status = 'normalization_pending'"
                    " WHERE processing_status = 'normalization_failed'"
                    f" LIMIT {_NORM_RESET_LIMIT}"
                )
            )
            db.commit()
            rows = result.rowcount
            logger.info("healer: reset %d normalization_failed records", rows)
            return HealResult(
                service="queues",
                outcome=HealOutcome.RECOVERED if rows > 0 else HealOutcome.SKIPPED,
                detail=f"Reset {rows} normalization_failed → normalization_pending",
                rows_affected=rows,
            )
        except Exception as exc:
            db.rollback()
            logger.warning("healer: normalization reset failed: %s", exc)
            return HealResult(
                service="queues",
                outcome=HealOutcome.FAILED,
                detail="normalization_failed reset raised an exception",
                error=str(exc),
            )


class BullMQStalledCleaner:
    """Move stalled BullMQ jobs from the stalled set back to the wait list.

    BullMQ marks jobs stalled when a worker locks a job but the lock expires
    before the job completes. Moving them back to wait allows healthy workers
    to pick them up again.
    """

    name = "clear_bullmq_stalled"

    def can_heal(self, health: ServiceHealth) -> bool:
        if health.name not in {"bullmq", "queues"}:
            return False
        counts = health.evidence.get("counts", {})
        return int(counts.get("stalled", 0)) > 0

    def heal(self, db: Session) -> HealResult:  # noqa: ARG002 — db unused here
        try:
            import redis as redis_lib

            from core.config import settings

            client = redis_lib.from_url(
                settings.redis_url, socket_connect_timeout=2, decode_responses=True
            )
            moved = 0
            for stalled_key in client.scan_iter(match="bull:*:stalled", count=50):
                queue_name = stalled_key.split(":")[1]
                wait_key = f"bull:{queue_name}:wait"
                while moved < _BULLMQ_STALLED_LIMIT:
                    job_id = client.spop(stalled_key)
                    if job_id is None:
                        break
                    client.lpush(wait_key, job_id)
                    moved += 1

            logger.info("healer: moved %d stalled BullMQ jobs back to wait", moved)
            return HealResult(
                service="bullmq",
                outcome=HealOutcome.RECOVERED if moved > 0 else HealOutcome.SKIPPED,
                detail=f"Moved {moved} stalled jobs back to wait queue",
                rows_affected=moved,
            )
        except Exception as exc:
            logger.warning("healer: bullmq stalled clear failed: %s", exc)
            return HealResult(
                service="bullmq",
                outcome=HealOutcome.FAILED,
                detail="stalled job cleanup raised an exception",
                error=str(exc),
            )


# Registry — ordered by priority (cheapest/safest first).
_HEALERS: list[Healer] = [
    NormalizationBacklogHealer(),
    BullMQStalledCleaner(),
]


def find_healer(health: ServiceHealth) -> Healer | None:
    for healer in _HEALERS:
        if healer.can_heal(health):
            return healer
    return None


def run_healers(health_list: list[ServiceHealth], db: Session) -> list[HealResult]:
    """Attempt healing for every unhealthy service that has a registered healer."""
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
        logger.info("healer: running %s for service=%s", healer.name, item.name)
        results.append(healer.heal(db))
    return results

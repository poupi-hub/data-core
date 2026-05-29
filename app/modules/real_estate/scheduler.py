import logging
import time
import uuid
from datetime import datetime, timezone

from app.modules.real_estate.collectors import ApolarCollector
from database.session import SessionLocal
from scheduler.async_runner import run_async

logger = logging.getLogger(__name__)


def run_real_estate_daily_collection() -> None:
    """Daily scheduled job: collect real estate listings via ApolarCollector (Playwright).

    Emits a standardized operational log entry on completion (Phase D Fase 5):
    run_id, domain, source, status, duration_ms, collected_count, persisted_count, failed_count.
    """
    run_id = str(uuid.uuid4())
    started_at = time.monotonic()
    logger.info(
        "Job run started",
        extra={
            "run_id": run_id,
            "job": "run_real_estate_daily_collection",
            "domain": "real_estate",
            "source": "apolar",
        },
    )

    collected = 0
    persisted = 0
    failed = 0
    error: Exception | None = None

    async def _run() -> None:
        nonlocal collected, persisted, failed
        db = SessionLocal()
        try:
            result = await ApolarCollector(db).run()
            # ApolarCollector.run() returns RealEstateCollectorResult (frozen dataclass):
            #   discovered_urls, collected_listings, invalid_urls, errors, elapsed_seconds
            # Map to standard observability fields.
            collected = int(getattr(result, "discovered_urls", 0))
            persisted = int(getattr(result, "collected_listings", 0))
            failed = int(getattr(result, "errors", 0))
            logger.debug("Real estate collector raw result", extra={"run_id": run_id, **result.__dict__})
        finally:
            db.close()

    try:
        run_async(_run())
    except Exception as exc:
        error = exc
        raise
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        now_iso = datetime.now(timezone.utc).isoformat()
        status = "error" if error else ("partial" if failed > 0 else "success")
        extra = {
            "run_id": run_id,
            "job": "run_real_estate_daily_collection",
            "domain": "real_estate",
            "source": "apolar",
            "status": status,
            "duration_ms": duration_ms,
            "collected_count": collected,
            "persisted_count": persisted,
            "normalized_count": 0,
            "failed_count": failed,
            "retry_count": 0,
        }
        if error:
            extra["last_failure_at"] = now_iso
            extra["error"] = str(error)
            logger.error("Job run finished", extra=extra)
        else:
            extra["last_success_at"] = now_iso
            logger.info("Job run finished", extra=extra)


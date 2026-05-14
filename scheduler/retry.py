import logging
import time
import traceback as tb
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def with_retry(
    fn: Callable[[], Any],
    *,
    job_name: str,
    max_retries: int = 2,
    backoff_seconds: float = 15.0,
) -> Any:
    """Run fn with up to max_retries retries and exponential backoff.

    On final failure, writes a JobDeadLetter record to CollectorError and re-raises.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 2):  # attempt 1..max_retries+1
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt <= max_retries:
                wait = backoff_seconds * attempt
                logger.warning(
                    "Job failed, retrying",
                    extra={"job": job_name, "attempt": attempt, "retry_in_seconds": wait, "error": str(exc)},
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Job exhausted retries, writing dead letter",
                    extra={"job": job_name, "attempts": attempt, "error": str(exc)},
                )

    _write_dead_letter(job_name, last_exc, max_retries)
    raise last_exc  # type: ignore[misc]


def _write_dead_letter(job_name: str, exc: Exception | None, max_retries: int) -> None:
    from api.metrics import job_dead_letters_total
    from database.models import CollectorError
    from database.session import SessionLocal

    job_dead_letters_total.labels(job_name=job_name).inc()

    db = SessionLocal()
    try:
        db.add(
            CollectorError(
                collector_name=job_name,
                error_type="JobDeadLetter",
                message=str(exc) if exc else "unknown error",
                traceback=tb.format_exc() if exc else None,
                context={"max_retries": max_retries, "attempts": max_retries + 1},
            )
        )
        db.commit()
    except Exception as write_exc:  # noqa: BLE001
        logger.error("Failed to write dead letter record", extra={"job": job_name, "error": str(write_exc)})
    finally:
        db.close()

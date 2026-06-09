import logging
import time
from collections.abc import Callable
from typing import TypeVar

from app.auto_healing.scheduler import auto_healing_watchdog_job
from app.runtime.scheduler_heartbeat import record_job_execution
from app.runtime.scheduler_reliability import SchedulerReliabilityEngine
from core.config import settings
from scheduler.jobs import (
    alert_webhook_job,
    analytics_job,
    compute_dataset_integrity_job,
    compute_source_health_job,
    data_retention_job,
    dataset_quality_crypto_job,
    normalize_job,
    operational_watchdog_job,
    poupi_baby_coverage_intelligence_job,
    run_ecommerce_url_targets_job,
    signal_outcomes_job,
    take_daily_snapshot_job,
)
from scheduler.retry import with_retry

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _with_heartbeat(job_name: str, fn: Callable[[], T]) -> T:
    t = time.monotonic()
    status = "success"
    try:
        return fn()
    except Exception:
        status = "error"
        raise
    finally:
        duration = time.monotonic() - t
        try:
            record_job_execution(
                job_name,
                status=status,
                duration_seconds=round(duration, 3),
                scheduled_at=t,
            )
        except Exception as exc:
            logger.warning("scheduler_heartbeat: record failed for %s: %s", job_name, exc)


def _normalize_with_configured_module(limit: int = 100) -> None:
    normalize_job(module=settings.scheduler_pipeline_module, limit=limit)


def _analytics_with_configured_module(limit: int = 100) -> None:
    analytics_job(module=settings.scheduler_pipeline_module, limit=limit)


def _run_normalize_reliability_engine() -> None:
    SchedulerReliabilityEngine().run(
        "normalize_job",
        _normalize_with_configured_module,
        priority="HIGH",
        supports_limit=True,
        default_limit=settings.scheduler_reliability_base_batch_size,
    )


def _run_analytics_reliability_engine() -> None:
    SchedulerReliabilityEngine().run(
        "analytics_job",
        _analytics_with_configured_module,
        priority="NORMAL",
        supports_limit=True,
        default_limit=settings.scheduler_reliability_base_batch_size,
    )


def _run_alert_webhook_reliability_engine() -> None:
    SchedulerReliabilityEngine().run(
        "alert_webhook_job",
        alert_webhook_job,
        priority="LOW",
    )


def _run_normalize_with_retry() -> None:
    with_retry(_run_normalize_reliability_engine, job_name="normalize_job")


def _run_analytics_with_retry() -> None:
    with_retry(_run_analytics_reliability_engine, job_name="analytics_job")


def _run_alert_webhook_with_retry() -> None:
    with_retry(_run_alert_webhook_reliability_engine, job_name="alert_webhook_job")


def _run_auto_healing_watchdog_with_retry() -> None:
    with_retry(auto_healing_watchdog_job, job_name="auto_healing_watchdog_job")


def _run_dataset_quality_crypto_with_retry() -> None:
    with_retry(dataset_quality_crypto_job, job_name="dataset_quality_crypto_job")


def _run_poupi_baby_coverage_intelligence_with_retry() -> None:
    with_retry(
        poupi_baby_coverage_intelligence_job,
        job_name="poupi_baby_coverage_intelligence_job",
    )


def _run_signal_outcomes_with_retry() -> None:
    with_retry(signal_outcomes_job, job_name="signal_outcomes_job")


def run_normalize_reliable() -> None:
    _with_heartbeat("normalize_job", _run_normalize_with_retry)


def run_analytics_reliable() -> None:
    _with_heartbeat("analytics_job", _run_analytics_with_retry)


def run_alert_webhook_reliable() -> None:
    _run_alert_webhook_with_retry()


def run_data_retention_reliable() -> None:
    SchedulerReliabilityEngine().run(
        "data_retention_job",
        data_retention_job,
        priority="LOW",
    )


def run_ecommerce_url_targets_reliable() -> None:
    SchedulerReliabilityEngine().run(
        "run_ecommerce_url_targets_job",
        run_ecommerce_url_targets_job,
        priority="NORMAL",
        supports_limit=True,
        default_limit=settings.scheduler_reliability_base_batch_size,
    )


def run_operational_watchdog_with_retry() -> None:
    with_retry(operational_watchdog_job, job_name="operational_watchdog_job")


def run_auto_healing_watchdog_reliable() -> None:
    _with_heartbeat("auto_healing_watchdog_job", _run_auto_healing_watchdog_with_retry)


def run_dataset_quality_crypto_reliable() -> None:
    _with_heartbeat("dataset_quality_crypto_job", _run_dataset_quality_crypto_with_retry)


def run_poupi_baby_coverage_intelligence_reliable() -> None:
    _with_heartbeat(
        "poupi_baby_coverage_intelligence_job",
        _run_poupi_baby_coverage_intelligence_with_retry,
    )


def run_signal_outcomes_reliable() -> None:
    _with_heartbeat("signal_outcomes_job", _run_signal_outcomes_with_retry)


def run_source_health_with_retry() -> None:
    with_retry(compute_source_health_job, job_name="compute_source_health_job")


def run_dataset_integrity_with_retry() -> None:
    with_retry(compute_dataset_integrity_job, job_name="compute_dataset_integrity_job")


def run_daily_snapshot_with_retry() -> None:
    with_retry(take_daily_snapshot_job, job_name="take_daily_snapshot_job")


def run_incident_history_aggregation() -> None:
    from app.incident_history.job import incident_history_aggregation_job
    with_retry(incident_history_aggregation_job, job_name="incident_history_aggregation_job")


def run_nba_quant_pipeline_reliable() -> None:
    from scheduler.jobs import nba_quant_pipeline_job
    with_retry(nba_quant_pipeline_job, job_name="nba_quant_pipeline_job")

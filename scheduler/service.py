import logging

from apscheduler.schedulers.background import BackgroundScheduler

from collectors.registry import registry
from core.config import settings
from app.modules.real_estate.scheduler import run_real_estate_daily_collection
from app.modules.sports_odds.scheduler import run_sports_odds_recurring_collection
from scheduler.jobs import alert_webhook_job, analytics_job, cleanup_stale_runs_job, collect_raw_job, data_retention_job, normalize_job, run_poupi_legacy_targets_job
from scheduler.retry import with_retry

logger = logging.getLogger(__name__)


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)

    if settings.scheduler_collectors_enabled:
        for collector_type in registry.all():
            metadata = collector_type.metadata
            scheduler.add_job(
                collect_raw_job,
                "interval",
                minutes=metadata.default_interval_minutes,
                args=[metadata.name],
                id=f"collector:{metadata.name}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

    scheduler.add_job(
        cleanup_stale_runs_job,
        "interval",
        minutes=15,
        id="maintenance:cleanup_stale_runs",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        lambda: with_retry(alert_webhook_job, job_name="alert_webhook_job"),
        "interval",
        hours=1,
        id="maintenance:alert_webhook",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        data_retention_job,
        "cron",
        day_of_week="sun",
        hour=2,
        minute=0,
        id="maintenance:data_retention",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    if settings.scheduler_pipeline_enabled:
        scheduler.add_job(
            lambda: with_retry(normalize_job, job_name="normalize_job"),
            "interval",
            minutes=15,
            id="pipeline:normalize",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: with_retry(analytics_job, job_name="analytics_job"),
            "interval",
            minutes=60,
            id="pipeline:analytics",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if settings.scheduler_domain_jobs_enabled:
        scheduler.add_job(
            run_poupi_legacy_targets_job,
            "interval",
            hours=8,
            id="ecommerce:poupi_legacy_targets",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        scheduler.add_job(
            run_real_estate_daily_collection,
            "cron",
            hour=3,
            minute=30,
            id="real_estate:daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        scheduler.add_job(
            run_sports_odds_recurring_collection,
            "interval",
            minutes=30,
            id="sports_odds:recurring",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    return scheduler


def start_scheduler(scheduler: BackgroundScheduler) -> None:
    if settings.scheduler_enabled and not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def stop_scheduler(scheduler: BackgroundScheduler) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

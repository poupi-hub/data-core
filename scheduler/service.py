import logging
import time

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler

from collectors.registry import registry
from core.config import settings
from app.modules.real_estate.scheduler import run_real_estate_daily_collection
from app.runtime.scheduler_heartbeat import boot_heartbeat, record_job_execution
from scheduler.jobs import (
    alert_webhook_job,
    analytics_job,
    cleanup_stale_runs_job,
    collect_raw_job,
    data_retention_job,
    normalize_job,
    operational_watchdog_job,
    run_ecommerce_url_targets_job,
    scheduler_heartbeat_job,
    watchdog_heartbeat_job,
)
from scheduler.retry import with_retry
from app.runtime.scheduler_reliability import SchedulerReliabilityEngine
from app.runtime.scheduler_watchdog import record_scheduler_execution_event

logger = logging.getLogger(__name__)


def _with_heartbeat(job_name: str, fn):
    """Wrap a no-arg callable with proof-of-execution heartbeat recording.

    Preserves all exceptions — never suppresses failures.
    """
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

# Nota: run_poupi_legacy_targets_job foi removido — substituído por run_ecommerce_url_targets_job (Phase B).
# Nota: run_sports_odds_recurring_collection foi removido do import — ver comentário abaixo.


def create_scheduler() -> BackgroundScheduler:
    # ── Phase 2: boot heartbeat (records scheduler_started_at once) ───────────
    try:
        boot_heartbeat()
    except Exception as exc:
        logger.warning("scheduler_heartbeat: boot failed (non-fatal): %s", exc)

    scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
    scheduler.add_listener(
        _record_scheduler_drift,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
    )
    reliability = SchedulerReliabilityEngine()

    if settings.scheduler_collectors_enabled:
        skipped = []
        for collector_type in registry.all():
            metadata = collector_type.metadata
            if not metadata.schedulable:
                # Collector marcado como schedulable=False — dado mock/demo, não agendar.
                # Ver: collectors/base.py CollectorMetadata.schedulable
                skipped.append(metadata.name)
                continue
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
        if skipped:
            logger.info(
                "Collectors não agendáveis (schedulable=False): %s",
                ", ".join(skipped),
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
        lambda: with_retry(
            lambda: reliability.run("alert_webhook_job", alert_webhook_job, priority="LOW"),
            job_name="alert_webhook_job",
        ),
        "interval",
        hours=1,
        id="maintenance:alert_webhook",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        lambda: reliability.run("data_retention_job", data_retention_job, priority="LOW"),
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
        pipeline_module = settings.scheduler_pipeline_module
        scheduler.add_job(
            lambda: _with_heartbeat(
                "normalize_job",
                lambda: with_retry(
                    lambda: reliability.run(
                        "normalize_job",
                        lambda limit: normalize_job(module=pipeline_module, limit=limit),
                        priority="HIGH",
                        supports_limit=True,
                        default_limit=settings.scheduler_reliability_base_batch_size,
                    ),
                    job_name="normalize_job",
                ),
            ),
            "interval",
            minutes=15,
            id="pipeline:normalize",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: _with_heartbeat(
                "analytics_job",
                lambda: with_retry(
                    lambda: reliability.run(
                        "analytics_job",
                        lambda limit: analytics_job(module=pipeline_module, limit=limit),
                        priority="NORMAL",
                        supports_limit=True,
                        default_limit=settings.scheduler_reliability_base_batch_size,
                    ),
                    job_name="analytics_job",
                ),
            ),
            "interval",
            minutes=60,
            id="pipeline:analytics",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if settings.scheduler_domain_jobs_enabled:
        # Ecommerce: scraping real de farmácias VTEX (17 targets ativos)
        scheduler.add_job(
            lambda: reliability.run(
                "run_ecommerce_url_targets_job",
                run_ecommerce_url_targets_job,
                priority="NORMAL",
                supports_limit=True,
                default_limit=settings.scheduler_reliability_base_batch_size,
            ),
            "interval",
            hours=2,
            id="ecommerce:url_scraper_targets",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Imóveis: coleta diária via Playwright (ApolarCollector)
        # Requer Playwright instalado no container do scheduler.
        scheduler.add_job(
            lambda: reliability.run(
                "run_real_estate_daily_collection",
                run_real_estate_daily_collection,
                priority="LOW",
            ),
            "cron",
            hour=3,
            minute=30,
            id="real_estate:daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Sports odds: DESATIVADO — NbaOddsCollector usa base_url="https://example.com"
        # (sem endpoints reais configurados). Reativar quando uma fonte real for integrada.
        # Para reativar:
        #   1. Implementar um collector concreto com URL real (ex: TheOddsAPI, BetAPI)
        #   2. Restaurar o import de run_sports_odds_recurring_collection
        #   3. Descomentar o bloco abaixo
        #
        # from app.modules.sports_odds.scheduler import run_sports_odds_recurring_collection
        # scheduler.add_job(
        #     run_sports_odds_recurring_collection,
        #     "interval",
        #     minutes=30,
        #     id="sports_odds:recurring",
        #     replace_existing=True,
        #     max_instances=1,
        #     coalesce=True,
        # )
        logger.debug(
            "sports_odds:recurring desativado — NbaOddsCollector sem fonte real configurada"
        )

    # ── Phase 2: Scheduler Proof-of-Execution Heartbeat (every 5 min) ────────
    scheduler.add_job(
        scheduler_heartbeat_job,
        "interval",
        minutes=5,
        id="platform:scheduler_heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Operational Watchdog ──────────────────────────────────────────────
    if settings.watchdog_enabled:
        scheduler.add_job(
            lambda: with_retry(operational_watchdog_job, job_name="operational_watchdog_job"),
            "interval",
            minutes=30,
            id="platform:operational_watchdog",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: with_retry(watchdog_heartbeat_job, job_name="watchdog_heartbeat_job"),
            "interval",
            hours=settings.watchdog_heartbeat_hours,
            id="platform:watchdog_heartbeat",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    return scheduler


def _record_scheduler_drift(event: object) -> None:
    """Persist APScheduler drift evidence without changing job execution."""
    job_id = getattr(event, "job_id", None)
    scheduled_run_time = getattr(event, "scheduled_run_time", None)
    exception = getattr(event, "exception", None)
    event_name = "job_executed"
    if getattr(event, "code", None) == EVENT_JOB_MISSED:
        event_name = "job_missed"
    elif exception is not None:
        event_name = "job_error"
    record_scheduler_execution_event(
        event=event_name,
        job_id=job_id,
        scheduled_run_time=scheduled_run_time,
        exception=str(exception) if exception else None,
    )


def start_scheduler(scheduler: BackgroundScheduler) -> None:
    if settings.scheduler_enabled and not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def stop_scheduler(scheduler: BackgroundScheduler) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

import logging
import time

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.job import Job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_PAUSED

from app.auto_healing.scheduler import effective_auto_healing_interval_minutes
from app.runtime.scheduler_heartbeat import boot_heartbeat, record_job_execution
from app.runtime.scheduler_watchdog import record_scheduler_execution_event
from collectors.registry import registry
from core.config import settings
from scheduler.job_wrappers import (
    run_alert_webhook_reliable,
    run_analytics_reliable,
    run_auto_healing_watchdog_reliable,
    run_daily_snapshot_with_retry,
    run_data_retention_reliable,
    run_dataset_integrity_with_retry,
    run_dataset_quality_crypto_reliable,
    run_ecommerce_url_targets_reliable,
    run_incident_history_aggregation,
    run_global_auto_health_daily,
    run_nba_quant_pipeline_reliable,
    run_normalize_reliable,
    run_operational_watchdog_with_retry,
    run_poupi_baby_coverage_intelligence_reliable,
    run_signal_outcomes_reliable,
    run_source_health_with_retry,
)
from scheduler.jobs import (
    cleanup_stale_runs_job,
    collect_raw_job,
    scheduler_heartbeat_job,
)

logger = logging.getLogger(__name__)


def mask_url(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://***:***@{host}"


def create_scheduler_jobstores() -> dict[str, SQLAlchemyJobStore]:
    jobstore_url = settings.scheduler_jobstore_url or settings.database_url
    return {
        "default": SQLAlchemyJobStore(
            url=jobstore_url,
            tablename=settings.scheduler_jobstore_table,
        )
    }


def create_configured_scheduler() -> BackgroundScheduler:
    if not settings.scheduler_jobstore_enabled:
        return create_scheduler()

    jobstore_url = settings.scheduler_jobstore_url or settings.database_url
    logger.info(
        "Scheduler SQLAlchemyJobStore enabled",
        extra={
            "jobstore_url": mask_url(jobstore_url),
            "jobstore_table": settings.scheduler_jobstore_table,
        },
    )
    return create_scheduler(
        jobstores=create_scheduler_jobstores(),
        start_paused_for_persistence=True,
    )


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


# Nota: run_poupi_legacy_targets_job foi removido.
# Substituído por run_ecommerce_url_targets_job (Phase B).
# Nota: run_sports_odds_recurring_collection foi removido do import — ver comentário abaixo.


def _add_job_preserving_persisted(scheduler: BackgroundScheduler, *args, **kwargs) -> Job:
    job_id = kwargs.get("id")
    kwargs.setdefault("misfire_grace_time", settings.scheduler_misfire_grace_seconds)
    if scheduler.running and job_id:
        existing = scheduler.get_job(job_id)
        if existing is not None:
            updates = {
                key: value
                for key, value in {
                    "coalesce": kwargs.get("coalesce"),
                    "max_instances": kwargs.get("max_instances"),
                    "misfire_grace_time": kwargs.get("misfire_grace_time"),
                }.items()
                if value is not None and getattr(existing, key) != value
            }
            if updates:
                existing.modify(**updates)
            logger.debug("Scheduler job already persisted; preserving next_run_time: %s", job_id)
            return scheduler.get_job(job_id) or existing
    kwargs.setdefault("replace_existing", not scheduler.running)
    return scheduler.add_job(*args, **kwargs)


def create_scheduler(
    *,
    jobstores: dict | None = None,
    start_paused_for_persistence: bool = False,
) -> BackgroundScheduler:
    # ── Phase 2: boot heartbeat (records scheduler_started_at once) ───────────
    try:
        boot_heartbeat()
    except Exception as exc:
        logger.warning("scheduler_heartbeat: boot failed (non-fatal): %s", exc)

    scheduler_kwargs = {"timezone": settings.scheduler_timezone}
    if jobstores is not None:
        scheduler_kwargs["jobstores"] = jobstores
    scheduler = BackgroundScheduler(**scheduler_kwargs)
    scheduler.add_listener(
        _record_scheduler_drift,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
    )
    if start_paused_for_persistence:
        scheduler.start(paused=True)

    if settings.scheduler_collectors_enabled:
        skipped = []
        for collector_type in registry.all():
            metadata = collector_type.metadata
            if not metadata.schedulable:
                # Collector marcado como schedulable=False — dado mock/demo, não agendar.
                # Ver: collectors/base.py CollectorMetadata.schedulable
                skipped.append(metadata.name)
                continue
            _add_job_preserving_persisted(
                scheduler,
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

    _add_job_preserving_persisted(
        scheduler,
        cleanup_stale_runs_job,
        "interval",
        minutes=15,
        id="maintenance:cleanup_stale_runs",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _add_job_preserving_persisted(
        scheduler,
        run_alert_webhook_reliable,
        "interval",
        hours=1,
        id="maintenance:alert_webhook",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Incident History — agrega eventos resolvidos em histórico + patterns
    _add_job_preserving_persisted(
        scheduler,
        run_incident_history_aggregation,
        "interval",
        hours=1,
        minutes=15,  # offset de 15min para não colidir com alert_webhook
        id="maintenance:incident_history_aggregation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _add_job_preserving_persisted(
        scheduler,
        run_data_retention_reliable,
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
        _add_job_preserving_persisted(
            scheduler,
            run_normalize_reliable,
            "interval",
            minutes=15,
            id="pipeline:normalize",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _add_job_preserving_persisted(
            scheduler,
            run_analytics_reliable,
            "interval",
            minutes=60,
            id="pipeline:analytics",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if settings.scheduler_domain_jobs_enabled:
        # Ecommerce: scraping real de farmácias VTEX (17 targets ativos)
        _add_job_preserving_persisted(
            scheduler,
            run_ecommerce_url_targets_reliable,
            "interval",
            hours=2,
            id="ecommerce:url_scraper_targets",
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
    _add_job_preserving_persisted(
        scheduler,
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
        _add_job_preserving_persisted(
            scheduler,
            run_operational_watchdog_with_retry,
            "interval",
            minutes=30,
            id="platform:operational_watchdog",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Auto-Healing Watchdog (safe-by-default; job no-ops while disabled)
    _add_job_preserving_persisted(
        scheduler,
        run_auto_healing_watchdog_reliable,
        "interval",
        minutes=effective_auto_healing_interval_minutes(),
        id="platform:auto_healing_watchdog",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Dataset quality — candle freshness/coverage scoring (every 30 min) ────
    # Runs in both scheduler and worker containers (pipeline must be enabled to have
    # normalized candles to score; but quality scoring itself is lightweight enough
    # to run alongside any other pipeline job).
    if settings.scheduler_pipeline_enabled:
        _add_job_preserving_persisted(
            scheduler,
            run_dataset_quality_crypto_reliable,
            "interval",
            minutes=30,
            id="quality:dataset_quality_crypto",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _add_job_preserving_persisted(
            scheduler,
            run_poupi_baby_coverage_intelligence_reliable,
            "interval",
            minutes=30,
            id="quality:poupi_baby_coverage_intelligence",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _add_job_preserving_persisted(
            scheduler,
            run_signal_outcomes_reliable,
            "interval",
            minutes=60,
            id="quality:signal_outcomes",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # ── NBA Quant — daily incremental update at 09:00 BRT (12:00 UTC) ────────
    # Skipped when ENABLE_SPORTS=false (sports module archived).
    if settings.scheduler_domain_jobs_enabled and settings.enable_sports:
        _add_job_preserving_persisted(
            scheduler,
            run_nba_quant_pipeline_reliable,
            "cron",
            hour=12,
            minute=0,
            id="nba:quant_pipeline_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    elif not settings.enable_sports:
        logger.info("sports archived: nba:quant_pipeline_daily not scheduled (ENABLE_SPORTS=false)")

    # ── FASE 3/4/5 — Source Health, Dataset Integrity, Daily Snapshots ───────
    # compute_source_health_job   : a cada 4h — saude operacional por coletor
    # compute_dataset_integrity_job : a cada 6h — scores de integridade por dataset
    # take_daily_snapshot_job     : 1x/dia (00:30 UTC) — snapshot longitudinal
    _add_job_preserving_persisted(
        scheduler,
        run_source_health_with_retry,
        "interval",
        hours=4,
        id="observability:source_health",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _add_job_preserving_persisted(
        scheduler,
        run_dataset_integrity_with_retry,
        "interval",
        hours=6,
        id="observability:dataset_integrity",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _add_job_preserving_persisted(
        scheduler,
        run_daily_snapshot_with_retry,
        "cron",
        hour=0,
        minute=30,
        id="observability:daily_snapshot",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _add_job_preserving_persisted(
        scheduler,
        run_global_auto_health_daily,
        "cron",
        hour=7,
        minute=0,
        id="observability:global_auto_health_daily",
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
    # Return freed memory to the OS after each job. Python's pymalloc pool
    # retains pages indefinitely; calling gc + malloc_trim releases them.
    # With PYTHONMALLOC=malloc (set in scheduler env) this covers all allocations.
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def start_scheduler(scheduler: BackgroundScheduler) -> None:
    if not settings.scheduler_enabled:
        return
    if scheduler.running and scheduler.state == STATE_PAUSED:
        scheduler.resume()
        logger.info("Scheduler resumed")
    elif not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def stop_scheduler(scheduler: BackgroundScheduler) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

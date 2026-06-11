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
    from core.config import settings
    if not settings.enable_sports:
        import logging
        logging.getLogger(__name__).info("sports archived: nba_quant_pipeline_job skipped (ENABLE_SPORTS=false)")
        return
    from scheduler.jobs import nba_quant_pipeline_job
    with_retry(nba_quant_pipeline_job, job_name="nba_quant_pipeline_job")


def run_global_auto_health_daily() -> None:
    """Coleta GlobalAutoHealth e envia resumo Telegram diário (07:00 UTC).

    Alerta imediato somente se status = INVESTIGAR ou BLOCKED.
    Reutiliza CriticalNotifier do auto_healing e operational_chat_id.
    """
    import httpx
    from app.global_auto_health.aggregator import run_global_auto_health
    from core.config import settings

    _log = logging.getLogger(__name__)

    result = run_global_auto_health()
    status = result["status"]
    checked_at = result["checked_at"]
    components = result.get("components", {})

    # ── Formatar resumo ────────────────────────────────────────────────────────
    _ICON = {"READY": "✅", "DEGRADED": "⚠️", "INVESTIGAR": "🔍", "BLOCKED": "🚨", "ARCHIVED": "🗄️"}
    lines = [f"<b>GlobalAutoHealth</b> — {status}", f"<i>{checked_at}</i>", ""]
    for name, comp in components.items():
        s = comp.get("status", "?")
        icon = _ICON.get(s, "?")
        detail = comp.get("detail", "")
        lines.append(f"{icon} <b>{name}</b>: {s}" + (f" — {detail}" if detail else ""))

    text = "\n".join(lines)

    # ── Enviar via Telegram ────────────────────────────────────────────────────
    token = getattr(settings, "telegram_bot_token", "")
    chat_id = (
        getattr(settings, "operational_chat_id", "")
        or getattr(settings, "telegram_chat_id", "")
    )
    if not token or not chat_id:
        _log.info("global_auto_health_daily: telegram not configured, skipping send")
        return

    # Diário: sempre envia. Imediato extra somente para INVESTIGAR/BLOCKED.
    _send_telegram(token, chat_id, text)
    _log.info("global_auto_health_daily: sent, status=%s", status)


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    import logging
    _log = logging.getLogger(__name__)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        import httpx
        httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10.0,
        )
    except Exception as exc:
        _log.warning("global_auto_health: telegram send failed: %s", exc)

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "data-core"
    app_env: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = Field(
        default="postgresql+psycopg://data_core:data_core@localhost:5432/data_core"
    )
    auto_create_tables: bool = False

    scheduler_enabled: bool = True
    scheduler_collectors_enabled: bool = True
    scheduler_pipeline_enabled: bool = True
    scheduler_pipeline_module: str | None = None
    scheduler_domain_jobs_enabled: bool = True
    scheduler_timezone: str = "America/Sao_Paulo"
    scheduler_jobstore_enabled: bool = False
    scheduler_jobstore_url: str | None = None
    scheduler_jobstore_table: str = "apscheduler_jobs"
    scheduler_misfire_grace_seconds: int = 30
    worker_concurrency: int = 2
    worker_pipeline_interval_seconds: int = 300

    collector_default_max_retries: int = 3
    collector_default_retry_delay_seconds: int = 10

    # Auth
    api_key_enabled: bool = False
    api_key: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = False

    # Alerting (webhook)
    alert_webhook_url: str = ""
    alert_webhook_raw_freshness_hours: int = 24
    alert_webhook_raw_pending_minutes: int = 60
    alert_webhook_analytics_pending_minutes: int = 120

    # Telegram — used by operational_watchdog for alerts and heartbeat
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # legacy fallback; prefer channel-specific IDs below

    # Canal centralizado (legado) — todos os alertas vão para este chat_id
    # Deixar vazio usa telegram_chat_id como fallback
    telegram_system_chat_id: str = ""

    # ── Canais multi-chat Alfredo (Phase 12) ────────────────────────────────
    # Cada canal tem seu próprio chat_id. Se vazio, cai no fallback legado.
    # Env vars: BUSINESS_CHAT_ID, OPERATIONAL_CHAT_ID, EXECUTIVE_CHAT_ID, CRITICAL_CHAT_ID
    business_chat_id: str = ""    # descobertas, shadow signals, oportunidades
    operational_chat_id: str = "" # resumos diários, readiness, scheduler
    executive_chat_id: str = ""   # marcos de edge, gate n>=30/100, relatórios executivos
    critical_chat_id: str = ""    # falhas de infra, colapso de métricas

    # Watchdog thresholds
    watchdog_collection_stale_hours: int = 3  # alert if no new raw in this window
    watchdog_normalization_backlog_minutes: int = 45  # alert if raw pending > this
    watchdog_publication_stale_hours: int = 6  # alert if no Telegram publication
    watchdog_heartbeat_hours: int = 6  # send heartbeat every N hours
    watchdog_quality_score_threshold: int = 50  # alert if avg quality below this
    watchdog_anti_bot_hourly_threshold: int = 3  # alert if anti-bot rate > N/h
    watchdog_enabled: bool = True

    # Scheduler reliability protection layer. Defaults are observe-only.
    scheduler_reliability_enabled: bool = False
    scheduler_reliability_dry_run: bool = True
    scheduler_reliability_base_batch_size: int = 100
    scheduler_reliability_conservative_batch_size: int = 75
    scheduler_reliability_protective_batch_size: int = 50
    scheduler_reliability_critical_batch_size: int = 25
    scheduler_reliability_conservative_cooldown_seconds: float = 2.0
    scheduler_reliability_protective_cooldown_seconds: float = 5.0
    scheduler_reliability_critical_cooldown_seconds: float = 10.0
    scheduler_reliability_low_priority_extra_delay_seconds: float = 10.0

    # poupi-baby URL (used by watchdog to optionally query poupi-baby health)
    poupi_baby_url: str = ""

    # Adaptive Policy Contract (Phase 10)
    # Rollout phases: 1=OBSERVE_ONLY, 2=WARN_ONLY, 3=SAFE_MODE_HINTS, 4=FAIL_CLOSED_CRITICAL_ONLY
    adaptive_policy_rollout_phase: int = 1
    adaptive_policy_enabled: bool = True

    # Telegram Longitudinal Summary (Phase 11)
    # Master switch: must be True AND telegram_enabled=True for any message to be sent.
    # Type-specific flags allow disabling individual summary categories independently.
    telegram_summary_enabled: bool = False
    telegram_summary_operational_enabled: bool = True  # hourly operational health
    telegram_summary_quant_enabled: bool = True  # 6h quant/adaptive intelligence
    telegram_summary_longitudinal_enabled: bool = True  # daily 24h vs 7d digest
    telegram_summary_alerts_enabled: bool = True  # immediate alerts (with cooldown)
    # Cron hour for the daily longitudinal digest (UTC, 0-23)
    telegram_summary_longitudinal_cron_hour: int = 8

    # Auto-Healing Watchdog (safe-by-default operational routine)
    auto_healing_enabled: bool = False
    auto_healing_dry_run: bool = True
    auto_healing_interval_minutes: int = 120
    auto_healing_telegram_report: bool = True
    auto_healing_history_path: str = "runtime-data/auto_healing_watchdog.jsonl"
    auto_healing_history_max_mb: int = 10
    auto_healing_alert_window_hours: int = 24
    auto_healing_telegram_cooldown_minutes: int = 120
    auto_healing_service_urls: str = ""
    poupi_crypto_internal_url: str = "http://poupi-crypto-api:8002"

    # Sports module master switch (NBA + WNBA + sports_odds).
    # Set ENABLE_SPORTS=false to park all sports jobs, routers, and Telegram alerts
    # without deleting any data or code.  Historical data remains queryable via DB.
    enable_sports: bool = True
    # NBA Telegram simulation alerts (only fires when enable_sports=true too)
    enable_nba_telegram_simulations: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

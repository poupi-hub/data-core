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
    scheduler_domain_jobs_enabled: bool = True
    scheduler_timezone: str = "America/Sao_Paulo"
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
    telegram_chat_id: str = ""  # alert/ops chat; can be group or personal

    # Watchdog thresholds
    watchdog_collection_stale_hours: int = 3       # alert if no new raw in this window
    watchdog_normalization_backlog_minutes: int = 45  # alert if raw pending > this
    watchdog_publication_stale_hours: int = 6      # alert if no Telegram publication
    watchdog_heartbeat_hours: int = 6              # send heartbeat every N hours
    watchdog_quality_score_threshold: int = 50     # alert if avg quality below this
    watchdog_anti_bot_hourly_threshold: int = 3    # alert if anti-bot rate > N/h
    watchdog_enabled: bool = True

    # poupi-baby URL (used by watchdog to optionally query poupi-baby health)
    poupi_baby_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

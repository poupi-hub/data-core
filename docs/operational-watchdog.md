# Operational Watchdog — Architecture & Flow

> **Purpose**: Monitor the Poupi data platform end-to-end during operator absence.
> Detects failures in collection, normalization, scraper quality, and Telegram publication
> and alerts immediately via Telegram.

---

## 1. Overview

```
APScheduler (every 30 min)
  └─ operational_watchdog_job()
       └─ WatchdogService.run()
            ├─ CollectionHealthChecker      → raw_collections freshness
            ├─ NormalizationHealthChecker   → raw→normalized conversion
            ├─ ScraperQualityChecker        → quality scores + anti-bot + drift
            └─ TelegramPublicationChecker   → last Telegram post age

APScheduler (every 6h)
  └─ watchdog_heartbeat_job()
       └─ WatchdogService.heartbeat()
            └─ runs all checks → HeartbeatFormatter → TelegramNotifier.send()
```

Each run:
1. Runs all 4 checkers in sequence
2. Sends immediate Telegram for each **critical** alert
3. Persists a `WatchdogRun` record to PostgreSQL
4. Updates Prometheus gauges

---

## 2. File Map

| File | Role |
|------|------|
| `app/watchdog/__init__.py` | Package marker |
| `app/watchdog/models.py` | `WatchdogRun` + `TelegramPublicationEvent` ORM models |
| `app/watchdog/checks/__init__.py` | `WatchdogAlert` + `CheckResult` dataclasses |
| `app/watchdog/checks/collection.py` | `CollectionHealthChecker` |
| `app/watchdog/checks/normalization.py` | `NormalizationHealthChecker` |
| `app/watchdog/checks/scraper_quality.py` | `ScraperQualityChecker` |
| `app/watchdog/checks/telegram_pub.py` | `TelegramPublicationChecker` |
| `app/watchdog/heartbeat.py` | `HeartbeatFormatter` + `format_alert_message` |
| `app/watchdog/notifier.py` | `TelegramNotifier` (httpx sync) |
| `app/watchdog/service.py` | `WatchdogService` orchestrator |
| `app/watchdog/api.py` | FastAPI router `/api/v1/watchdog/` |
| `scheduler/jobs.py` | `operational_watchdog_job`, `watchdog_heartbeat_job` |
| `alembic/versions/0018_watchdog_tables.py` | DB migration |

---

## 3. Check Details

### 3.1 CollectionHealthChecker

**Source**: `raw_collections` table  
**Queries**:
- Rows collected in last `watchdog_collection_stale_hours` (default 3h) — grouped by source
- All distinct source names seen in last 7 days → identify "known sources"
- Rows in last 1h — for failure rate calculation

**Alert codes**:
| Code | Severity | Condition |
|------|----------|-----------|
| `collection_stale` | critical | known source with no collection in last N hours |
| `collection_platform_down` | critical | NO source collected in last N hours (but known sources exist) |
| `collection_high_failure_rate` | warning | domain with >40% `normalization_failed` status in last 1h (min 3 samples) |

**Metrics emitted**:
- `domain_stats` — per-source stats within the window
- `known_sources_count`, `active_sources_last_window`, `stale_sources`
- `active_target_count` — from `collection_targets`
- `last_raw_collection_age_seconds` — age of most recent raw record

---

### 3.2 NormalizationHealthChecker

**Source**: `raw_collections` + `normalized_products` tables  
**Queries**:
- Count of `normalization_pending` older than threshold (default 45 min)
- Status distribution in last 24h per source → success rate
- Max `normalized_at` from `normalized_products`

**Alert codes**:
| Code | Severity | Condition |
|------|----------|-----------|
| `normalization_backlog` | warning/critical | >0 pending records older than threshold (critical if >20) |
| `normalization_low_success_rate` | warning | source with <70% normalized/total in last 24h (min 5 samples) |
| `normalization_stale` | warning | latest `normalized_products` record older than 4h |

---

### 3.3 ScraperQualityChecker

**Source**: `raw_collections.metadata_json` + `scraper_drift_events` table  
**Reads**:
- `metadata_json['quality']['score']` — quality score 0-100
- `metadata_json['anti_bot_detected']` — boolean flag
- `scraper_drift_events` — unresolved high/critical events from last 48h

**Alert codes**:
| Code | Severity | Condition |
|------|----------|-----------|
| `scraper_quality_low` | warning | source avg quality <50 in last 24h |
| `anti_bot_spike` | warning | source with ≥3 anti-bot detections in last 1h |
| `scraper_drift_detected` | warning/critical | open high/critical drift events (critical if any `risk_level=critical`) |

---

### 3.4 TelegramPublicationChecker

**Source**: `telegram_publication_events` table (populated via poupi-baby callback)

**Alert codes**:
| Code | Severity | Condition |
|------|----------|-----------|
| `telegram_publish_failing` | critical | failures exist + zero successes in last N hours |
| `telegram_no_publication_products_exist` | warning | no publication but normalized products exist (deal score too low) |
| `telegram_no_publication_no_data` | warning | no publication + no normalized products (collection issue) |
| `telegram_high_failure_rate` | warning | >30% failure rate in last 24h (min 3 samples) |

**If no callback data exists at all**: returns `ok` with informational note.

---

## 4. Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `operational_watchdog_status` | Gauge | `check` | 0=ok, 1=warning, 2=critical per check |
| `last_raw_collection_age_seconds` | Gauge | — | Age of newest raw record |
| `last_normalized_offer_age_seconds` | Gauge | — | Age of newest normalized product |
| `last_telegram_post_age_seconds` | Gauge | — | Age of last Telegram send |
| `raw_to_normalized_success_rate` | Gauge | — | Avg normalized/total across sources (24h) |
| `telegram_publish_success_total` | Counter | — | Watchdog Telegram sends that succeeded |
| `telegram_publish_failure_total` | Counter | — | Watchdog Telegram sends that failed |
| `domains_with_active_alerts` | Gauge | — | Count of source_names with open alerts |
| `watchdog_checks_total` | Counter | `status` | Count of watchdog runs by overall status |

---

## 5. REST API

Base: `/api/v1/watchdog/` (requires API key)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | Last watchdog run (status, alerts, metrics) |
| GET | `/runs?limit=20` | Run history |
| GET | `/alerts` | Alerts from last run |
| POST | `/heartbeat/send` | Trigger heartbeat manually |
| GET | `/telegram-events?limit=50` | Publication event history |
| POST | `/report/telegram-published` | poupi-baby callback to register sends |

### poupi-baby callback payload

```json
POST /api/v1/watchdog/report/telegram-published
{
  "group_id": "poupi-oportunidades",
  "product_id": "uuid-here",
  "offer_id": "optional-uuid",
  "marketplace": "drogasil",
  "price": 29.90,
  "deal_score": 82.5,
  "status": "sent",
  "fail_reason": null
}
```

---

## 6. Configuration (.env)

```env
# Telegram
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_CHAT_ID=<chat-id>

# Watchdog thresholds
WATCHDOG_ENABLED=true
WATCHDOG_COLLECTION_STALE_HOURS=3
WATCHDOG_NORMALIZATION_BACKLOG_MINUTES=45
WATCHDOG_PUBLICATION_STALE_HOURS=6
WATCHDOG_HEARTBEAT_HOURS=6
WATCHDOG_QUALITY_SCORE_THRESHOLD=50
WATCHDOG_ANTI_BOT_HOURLY_THRESHOLD=3

# Scheduler
SCHEDULER_ENABLED=true
```

---

## 7. End-to-End Data Flow

```
[Ecommerce Scraper]
  ↓ saves RawCollection with metadata_json={quality, anti_bot_detected}
  ↓
[raw_collections table]
  ↓ CollectionHealthChecker reads every 30 min
  ↓
[Normalization Pipeline]
  ↓ updates processing_status, creates NormalizedProduct
  ↓
[normalized_products table]
  ↓ NormalizationHealthChecker reads every 30 min
  ↓
[poupi-baby TelegramGroupProcessor]
  ↓ posts to Telegram → calls POST /api/v1/watchdog/report/telegram-published
  ↓
[telegram_publication_events table]
  ↓ TelegramPublicationChecker reads every 30 min
  ↓
[WatchdogService.run()]
  ↓ aggregates 4 CheckResults
  ↓ sends critical alerts via TelegramNotifier
  ↓ persists WatchdogRun
  ↓ updates Prometheus
```

---

## 8. Database Tables

### `watchdog_runs`
```sql
id              BIGSERIAL PRIMARY KEY
run_at          TIMESTAMPTZ NOT NULL
overall_status  VARCHAR(20) NOT NULL    -- ok/warning/critical
duration_ms     INTEGER
check_results   JSONB                   -- {collection: {...}, normalization: {...}, ...}
alert_codes     JSONB                   -- ["collection_stale", ...]
metrics_snapshot JSONB
telegram_sent   BOOLEAN DEFAULT FALSE
error_message   TEXT
```

### `telegram_publication_events`
```sql
id           BIGSERIAL PRIMARY KEY
group_id     VARCHAR(200)
product_id   UUID
offer_id     UUID
marketplace  VARCHAR(100)
price        NUMERIC(12,2)
deal_score   NUMERIC(5,2)
status       VARCHAR(50)    -- sent/failed/rate_limited
fail_reason  TEXT
published_at TIMESTAMPTZ NOT NULL
reported_by  VARCHAR(100)   -- "poupi-baby"
```

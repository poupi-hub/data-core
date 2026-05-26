# data-core — Observability Guide

> AI-friendly reference. Auto-contained. Updated: 2026-05-16.

---

## Architecture

```
┌─────────────────────────────────────────┐
│              data-core                  │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  Structured Logs (stdout)       │    │
│  │  • JSON (LOG_JSON=true)         │    │
│  │  • correlation_id per request   │    │
│  │  • trace_id per request         │    │
│  │  • pipeline_domain / stage      │    │
│  └─────────────────────────────────┘    │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  Prometheus /metrics            │    │
│  │  • HTTP instrumentation (auto)  │    │
│  │  • pipeline_stage_*             │    │
│  │  • collection_*                 │    │
│  │  • circuit_breaker_*            │    │
│  │  • db_pool_*                    │    │
│  └─────────────────────────────────┘    │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  PostgreSQL (pipeline_runs,     │    │
│  │  pipeline_failures)             │    │
│  │  • per-stage run history        │    │
│  │  • failure detail + traceback   │    │
│  └─────────────────────────────────┘    │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  Health endpoints               │    │
│  │  GET /health  (full check)      │    │
│  │  GET /ready   (readiness)       │    │
│  │  GET /live    (liveness)        │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
         │                    │
         ▼                    ▼
   Prometheus            Grafana Dashboards
   (scrape :8000)        data-core-ops-v1
         │                data-core-perf-v1
         ▼
   AlertManager
   (7 alert rules)
```

---

## Prometheus Metrics Reference

### HTTP (auto-instrumented by prometheus-fastapi-instrumentator)
| Metric | Labels | Description |
|---|---|---|
| `http_requests_total` | method, path, status | Total HTTP requests |
| `http_request_duration_seconds` | method, path | Request duration histogram |

### Pipeline stage metrics
| Metric | Labels | Description |
|---|---|---|
| `pipeline_stage_runs_total` | domain, stage, status | Total stage executions |
| `pipeline_stage_duration_seconds` | domain, stage | Duration histogram (buckets: 0.1→300s) |
| `pipeline_stage_active` | domain, stage | Currently in-flight stages (gauge) |
| `pipeline_stage_last_success_timestamp_seconds` | domain, stage | Unix epoch of last success |
| `pipeline_items_processed_total` | domain, stage | Items successfully processed |
| `pipeline_items_error_total` | domain, stage | Items that caused errors |

**domain** values: `crypto`, `ecommerce`, `real_estate`, `sports_betting`, `trading`  
**stage** values: `collection`, `normalization`, `analytics`  
**status** values: `success`, `error`

### Collection metrics
| Metric | Labels | Description |
|---|---|---|
| `collection_raw_saved_total` | domain, collector_name | Raw records saved |
| `collection_raw_duplicates_total` | domain, collector_name | Deduplication skips |
| `collection_errors_total` | domain, collector_name, error_type | Collection errors |
| `collection_duration_seconds` | domain, collector_name | Collector run duration |

### Reliability metrics
| Metric | Labels | Description |
|---|---|---|
| `job_dead_letters_total` | job_name | Dead-letter counter (collector) |
| `job_dead_letters_unresolved` | — | Unresolved dead letters (gauge, live DB query) |
| `circuit_breaker_opens_total` | module, source_name | Circuit opens counter |
| `circuit_breaker_open_sources` | — | Open circuits count (gauge, live DB query) |

### Database metrics
| Metric | Description |
|---|---|
| `db_pool_size` | Configured pool size |
| `db_pool_checked_out` | Connections in use |

### Legacy ecommerce
| Metric | Labels | Description |
|---|---|---|
| `price_feed_requests_total` | cursor_used | Price-feed requests |
| `price_feed_items_served_total` | store_name | Items returned |
| `price_feed_response_size_items` | — | Response size histogram |

---

## Alert Rules (`prometheus/rules/data-core-alerts.yml`)

| Alert | Condition | Severity | Meaning |
|---|---|---|---|
| `DataCoreApiDown` | `up{job="data-core-api"} == 0` | critical | API unreachable for >2 min |
| `DataCoreJobDeadLetterCreated` | `job_dead_letters_unresolved > 0` | critical | A job exhausted all retries |
| `DataCoreCircuitBreakerOpen` | `circuit_breaker_open_sources > 0` | critical | A source is deactivated |
| `DataCoreCircuitBreakerSpike` | `rate(circuit_breaker_opens_total[1h]) > 2` | warning | Multiple trip in 1h |
| `DataCorePriceFeedNoItemsServed` | `rate(price_feed_items_served_total[30m]) == 0` | warning | Ecommerce sync stalled |
| `DataCorePriceFeedRequestsDrop` | `rate(price_feed_requests_total[30m]) == 0` | warning | No consumers polling |
| `DataCoreDeadLetterSpike` | `rate(job_dead_letters_total[30m]) > 3` | warning | Multiple failures in 30 min |

**Recommended additions** (pipeline staleness):
```yaml
- alert: DataCorePipelineStaleCrypto
  expr: time() - pipeline_stage_last_success_timestamp_seconds{domain="crypto",stage="analytics"} > 3600
  for: 5m
  labels: { severity: warning }
  annotations:
    summary: "crypto analytics stale for >1h"

- alert: DataCorePipelineError
  expr: rate(pipeline_stage_runs_total{status="error"}[15m]) > 0
  for: 2m
  labels: { severity: warning }
  annotations:
    summary: "Pipeline stage errors: {{ $labels.domain }}/{{ $labels.stage }}"
```

---

## Structured Logs

### Format (LOG_JSON=false, plain text)
```
2026-05-16 02:19:40 | INFO | scheduler.jobs | Starting analytics job | cid=- tid=- | domain=crypto stage=analytics
```

### Format (LOG_JSON=true)
```json
{
  "timestamp": "2026-05-16 02:19:40,567",
  "level": "INFO",
  "logger": "scheduler.jobs",
  "message": "Starting analytics job",
  "correlation_id": "a3f9c21e-...",
  "trace_id": "b7d4e891-...",
  "pipeline_domain": "crypto",
  "pipeline_stage": "analytics",
  "pipeline_module": "crypto"
}
```

### Correlation IDs
- `X-Correlation-ID`: set by caller, propagated across services. If not set, generated server-side.
- `X-Trace-ID`: always generated server-side per request.
- Both echoed in response headers.
- Scheduler/worker jobs: `correlation_id="-"` (no HTTP request context).

### Setting pipeline context in new jobs
```python
from logs.config import set_pipeline_context, clear_pipeline_context

set_pipeline_context(domain="crypto", stage="collection")
try:
    # All log records inside here include domain/stage
    logger.info("Fetching candles", extra={"symbol": "BTC/USDT"})
finally:
    clear_pipeline_context()
```

---

## Health checks

### /health — Full dependency check
```bash
curl http://localhost:8000/health
# {"status":"ok","app":"data-core","environment":"production","dependencies":{"postgres":{"status":"ok"},"redis":{"status":"ok"}}}
```

### /live — Liveness (no DB)
```bash
curl http://localhost:8000/live
# {"status":"alive","app":"data-core"}
```

### /ready — Readiness (DB + Redis + Scheduler)
```bash
curl http://localhost:8000/ready
# 200: {"ready":true,"checks":{"postgres":"ok","redis":"ok","scheduler":"ok"}}
# 503: {"ready":false,"checks":{"postgres":"ok","redis":"error: Connection refused","scheduler":"ok"}}
```

---

## Database observability tables

### pipeline_runs
```sql
SELECT domain, stage, status, 
       ROUND(duration_seconds::numeric, 2) as dur_s,
       items_processed, items_error, started_at
FROM pipeline_runs
ORDER BY started_at DESC
LIMIT 20;
```

### pipeline_failures (last 24h)
```sql
SELECT domain, stage, error_type, 
       LEFT(error_message, 80) as msg,
       is_terminal, occurred_at
FROM pipeline_failures
WHERE occurred_at > NOW() - INTERVAL '24 hours'
ORDER BY occurred_at DESC;
```

### Average stage duration per domain (last 7 days)
```sql
SELECT domain, stage,
       COUNT(*) as runs,
       ROUND(AVG(duration_seconds)::numeric, 2) as avg_s,
       ROUND(MAX(duration_seconds)::numeric, 2) as max_s,
       SUM(items_processed) as total_processed,
       SUM(items_error) as total_errors
FROM pipeline_runs
WHERE started_at > NOW() - INTERVAL '7 days'
  AND status IN ('success', 'partial')
GROUP BY domain, stage
ORDER BY domain, stage;
```

---

## Grafana Dashboards

| Dashboard | UID | File | Description |
|---|---|---|---|
| data-core Ops | `data-core-ops-v1` | `docs/grafana-dashboard-data-core-ops.json` | Pipeline health, stage durations, DB runs, failures |
| data-core Perf | `data-core-perf-v1` | `docs/grafana-dashboard-data-core.json` | Collection volume, price feed, circuit breakers |
| poupi-crypto | `poupi-crypto-perf-v1` | `docs/grafana-dashboard-poupi-crypto.json` | Paper trading performance |

Import via:
```bash
curl -s -X POST \
  -u 'admin:PASSWORD' \
  -H 'Content-Type: application/json' \
  -d @docs/grafana-dashboard-data-core-ops.json \
  http://grafana:3000/api/dashboards/import
```

---

## Quickstart: diagnose a pipeline issue

```bash
# 1. Is the API up?
curl http://data-core-api:8000/live

# 2. Are dependencies healthy?
curl http://data-core-api:8000/health

# 3. Recent pipeline failures?
psql -U data_core_user -d data_core_db -c \
  "SELECT domain, stage, error_type, error_message, occurred_at FROM pipeline_failures ORDER BY occurred_at DESC LIMIT 10;"

# 4. Stale stages? (> 1h since last success)
psql -U data_core_user -d data_core_db -c \
  "SELECT domain, stage, status, started_at FROM pipeline_runs WHERE status='running' AND started_at < NOW() - INTERVAL '30 min';"

# 5. Circuit breakers?
psql -U data_core_user -d data_core_db -c \
  "SELECT collector_name, error_type, message, created_at FROM collector_errors WHERE error_type='CircuitOpen' AND resolved_at IS NULL;"

# 6. Dead letters?
psql -U data_core_user -d data_core_db -c \
  "SELECT collector_name, error_type, message, created_at FROM collector_errors WHERE error_type='JobDeadLetter' AND resolved_at IS NULL;"

# 7. Check Prometheus metrics directly
curl -s http://data-core-api:8000/metrics | grep pipeline_stage
```

---

## Dataset Quality Metrics (added 2026-05-26)

These metrics track the health of the OHLCV candle dataset per symbol/timeframe.
Emitted by `DatasetIntegrityScorer` every 30 minutes via `dataset_quality_crypto_job`.

### Metric Reference

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `dataset_integrity_score` | Gauge | `symbol`, `timeframe` | Composite integrity score 0-100. Combines freshness (40 pts), coverage (40 pts), OHLC consistency (20 pts). |
| `candle_coverage_pct` | Gauge | `symbol`, `timeframe` | Percentage of expected candles present in the last 24h (0-100). |
| `stale_candle_total` | Counter | `symbol`, `timeframe` | Total detections of stale candle data (last candle older than 2× expected interval). |
| `candle_gap_total` | Counter | `symbol`, `timeframe` | Total candle gaps detected (missing intervals in the last 24h). |

### Score Interpretation

| Score | Interpretation |
|-------|---------------|
| 80-100 | ✅ Healthy — full freshness + coverage + clean OHLC |
| 60-79 | ⚠️ Degraded — some gaps or slight staleness |
| 40-59 | ⚠️ Warning — significant gaps, possible collection delays |
| < 40 | ❌ Critical — pipeline failure or exchange downtime |

### Prometheus Queries

```promql
# Current integrity scores by pair
dataset_integrity_score

# Pairs below warning threshold
dataset_integrity_score < 60

# Coverage percentage for 1h candles
candle_coverage_pct{timeframe="1h"}

# Gap rate over last 2h
increase(candle_gap_total[2h])

# Stale detections in last hour
increase(stale_candle_total[1h]) > 0
```

### Grafana Dashboards

- **Dataset Quality — Crypto OHLCV** (uid: `dataset-quality-crypto`): Integrity scores, coverage %, stale/gap rates
- **Volatile vs Main — Signal Comparison** (uid: `volatile-comparison`): Side-by-side SOL/DOGE/XRP vs BTC/ETH

### Alert Rules

Located in `prometheus/rules/dataset_quality_alerts.yml`:

| Alert | Condition | Severity |
|-------|-----------|----------|
| `CryptoDatasetLowIntegrity` | `dataset_integrity_score < 50` for 15m | warning |
| `CryptoDatasetCriticallyDegraded` | `dataset_integrity_score < 20` for 5m | critical |
| `CryptoDatasetLowCoverage` | `candle_coverage_pct < 50` for 30m | warning |
| `CryptoDatasetMissingCoverage` | `candle_coverage_pct < 10` for 15m | critical |
| `CryptoDatasetGapSpike` | `increase(candle_gap_total[1h]) > 5` | warning |
| `CryptoDatasetStaleData` | `increase(stale_candle_total[30m]) > 0` | warning |

### REST API

```bash
# Signal outcomes (retrospective BUY/SELL evaluation)
GET /api/v1/trading/validation/signal-outcomes?symbol=SOL/USDT&limit=100

# Confidence calibration by decile
GET /api/v1/trading/validation/calibration?symbol=SOL/USDT

# Signal distribution drift detection
GET /api/v1/trading/validation/signal-drift?symbol=SOL/USDT&window_hours=24

# Manually trigger outcome evaluation
POST /api/v1/trading/validation/run-outcome-tracker?limit=200
```

### Database Tables

```sql
-- Dataset quality scores (last 30 days)
SELECT symbol, timeframe, integrity_score, coverage_pct, gap_count, evaluated_at
FROM crypto_dataset_quality_scores
WHERE evaluated_at > NOW() - INTERVAL '30 days'
ORDER BY evaluated_at DESC, symbol;

-- Signal outcomes (correctness by pair)
SELECT symbol, signal, outcome_correct, AVG(price_change_pct) as avg_change
FROM trading_signal_outcomes
WHERE signal_at > NOW() - INTERVAL '14 days'
GROUP BY symbol, signal, outcome_correct
ORDER BY symbol, signal;
```

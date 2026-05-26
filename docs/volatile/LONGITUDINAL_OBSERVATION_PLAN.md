# Longitudinal Observation Plan — poupi-crypto-volatile
**Runtime start:** 2026-05-26  
**Window:** 30 days (end: 2026-06-25)  
**Hypothesis:** SOL/DOGE/XRP generate richer trading datasets (more regime diversity, higher signal acceptance) than BTC/ETH at the same timeframes.

---

## Operational Overview

The volatile runtime runs in isolation from the main runtime:
- Separate DB, separate Redis, separate container network
- `DRY_RUN=true` hardcoded — advisory only, no live execution
- Symbols: SOL/USDT, DOGE/USDT, XRP/USDT
- Timeframes: 15m, 1h
- Collection: every 15 min via `crypto.crypto_coin_ohlcv` collector
- Signal evaluation: every 60 min via `signal_outcomes_job`
- Quality scoring: every 30 min via `dataset_quality_crypto_job`

---

## Phase 1 — Pipeline Validation (Days 0–3, 2026-05-26 to 2026-05-29)

**Goal:** confirm every stage of the pipeline is alive and producing data.

### Daily Checks

| Check | Tool | Pass Criterion |
|---|---|---|
| Collector running | `GET /health` + Grafana | `collect_raw_job` succeeds every 15 min |
| Normalization | `GET /api/v1/pipeline/status` | Pending raw < 50 |
| Analytics | `GET /api/v1/analytics/status` | TradingAnalytics rows created |
| Outcome tracker | `GET /api/v1/trading/validation/health` | `health_score > 0`, `last_evaluated_at` updated |
| Prometheus scraping | `GET /metrics` | `outcome_bootstrap_phase`, `dataset_maturity_score` visible |
| Dataset quality | Grafana `dataset_quality_crypto.json` | `dataset_integrity_score > 0` for all 3 symbols |

### Expected Behaviour (Day 0–3)

- `outcome_bootstrap_phase = 1` — all outcome-related alerts suppressed
- `dataset_maturity_score < 20` — BOOTSTRAP band expected
- All signals likely HOLD (insufficient candle history for 250-candle lookback)
- `candle_coverage_pct` rising from 0 toward 100% as candles accumulate
- `CryptoDatasetStale` alert may fire for first few hours until coverage builds

### Day 3 Checkpoint (`2026-05-29`)

```
GET /api/v1/trading/validation/health
→ last_evaluated_at: should be < 2h ago
→ total_outcomes: > 0 (first outcomes should be evaluated at day 0+6h)
→ health_score: > 40

GET /api/v1/trading/validation/readiness
→ bootstrap_mode: true (expected, <50 outcomes)
→ maturity_band: BOOTSTRAP or IMMATURE
```

---

## Phase 2 — Signal Accumulation (Days 3–7, 2026-05-29 to 2026-06-02)

**Goal:** measure HOLD dominance and first signs of signal diversity.

### Metrics to Watch

| Metric | Prometheus Query | Target |
|---|---|---|
| HOLD ratio | `rate(trading_signal_total{signal="HOLD"}[24h]) / rate(trading_signal_total[24h])` | < 95% by day 5 |
| BUY/SELL count | `increase(outcome_evaluated_total[24h])` | > 0 |
| Coverage | `candle_coverage_pct` | > 80% for all 3 symbols |
| Maturity score | `dataset_maturity_score` | > 20 (IMMATURE band) |
| Outcome lag | `outcome_runtime_lag_seconds` | < 3600 s |

### API Checks

```bash
# Signal drift: is recent distribution similar to historical?
GET /api/v1/trading/validation/signal-drift?symbol=SOL/USDT

# Are outcomes being produced?
GET /api/v1/trading/validation/signal-outcomes?limit=20

# Readiness (expect not ready yet)
GET /api/v1/trading/validation/readiness
```

### Day 7 Checkpoint (`2026-06-02`)

If `dataset_maturity_score < 20` persists after 7 days, investigate:
1. Are BUY/SELL signals being generated? (check `trading_signal_total`)
2. Are candles being normalized? (check `candle_coverage_pct`)
3. Is signal lookback (250 candles) being satisfied? (need ~3 days of 15m for 250 candles)
4. Is the volatile collector fetching the right source? (check `source` in normalized_market_candles)

---

## Phase 3 — Calibration Baseline (Days 7–14, 2026-06-02 to 2026-06-09)

**Goal:** accumulate ≥50 evaluated outcomes; run first calibration analysis.

### Readiness Criteria

```
calibration_ready = True when:
  - total_outcomes >= 50
  - non_hold_outcomes >= 10
  - distinct_regimes >= 3
  - distinct_symbols >= 3
```

### Calibration Analysis

Once `readiness.calibration_ready = True`:

```bash
# Per-symbol calibration
GET /api/v1/trading/validation/calibration?symbol=SOL/USDT
→ well_calibrated: bool (true = higher confidence → better accuracy)
→ calibration_slope: positive is good

GET /api/v1/trading/validation/calibration?symbol=DOGE/USDT
GET /api/v1/trading/validation/calibration?symbol=XRP/USDT

# Compare: do volatile symbols outperform main (BTC/ETH)?
GET /api/v1/trading/validation/calibration
```

### Hypothesis Validation Metrics (Day 14)

| Metric | Volatile Target | Main Baseline |
|---|---|---|
| HOLD ratio | < 90% | Expected ~95% |
| BUY/SELL outcome count | ≥ 10 | Reference |
| Accuracy (any symbol) | > 40% | Random = 50% (neutral) |
| Regime diversity | ≥ 3 distinct regimes | Reference |
| Calibration slope | > 0 (positive) | Reference |

**If volatile HOLD ratio ≤ main at day 14: hypothesis partially confirmed.**

### Day 14 Checkpoint (`2026-06-09`)

```
GET /api/v1/trading/validation/readiness
→ calibration_ready: true (expected)
→ maturity_band: USEFUL or CALIBRATION_READY

GET /api/v1/trading/validation/signal-drift
→ drift_detected: false = stable distribution (good)
→ dominated_by_hold: false = volatile hypothesis working (good)
```

---

## Phase 4 — Confidence Validation (Days 14–30, 2026-06-09 to 2026-06-25)

**Goal:** validate confidence calibration and measure MFE/MAE stability.

### Analysis Checklist

1. **Calibration slope**: is it positive? Does high confidence = better accuracy?
2. **MFE vs MAE**: `outcome_avg_mfe_pct > outcome_avg_mae_pct`? (favorable excursion > adverse)
3. **Signal drift stability**: does `signal-drift` stay below 20pp deviation?
4. **Symbol comparison**: which of SOL/DOGE/XRP has the best accuracy?
5. **Timeframe comparison**: 15m vs 1h — which generates better-calibrated signals?

### Outcome Analysis Queries

```sql
-- Accuracy by symbol
SELECT symbol, signal, COUNT(*) as total,
       SUM(CASE WHEN outcome_correct THEN 1 ELSE 0 END) as correct,
       AVG(price_change_pct) as avg_change,
       AVG(max_favorable_pct) as avg_mfe,
       AVG(ABS(max_adverse_pct)) as avg_mae
FROM trading_signal_outcomes
WHERE evaluated_at > NOW() - INTERVAL '7 days'
GROUP BY symbol, signal
ORDER BY symbol, signal;

-- Calibration check: confidence decile accuracy
SELECT (confidence / 10) * 10 as conf_bucket,
       COUNT(*) as total,
       SUM(CASE WHEN outcome_correct THEN 1 ELSE 0 END) as correct,
       ROUND(AVG(CASE WHEN outcome_correct THEN 1.0 ELSE 0.0 END), 3) as accuracy
FROM trading_signal_outcomes
GROUP BY 1 ORDER BY 1;
```

### Hypothesis Decision (Day 30: 2026-06-25)

The hypothesis is **CONFIRMED** if at least 2 of 3 criteria hold:
1. HOLD ratio for volatile symbols < HOLD ratio for main symbols (BTC/ETH)
2. At least one volatile symbol achieves accuracy > 40% over ≥20 evaluated outcomes
3. Calibration slope ≥ 0 (higher confidence does not anti-correlate with accuracy)

The hypothesis is **INCONCLUSIVE** if:
- Total non-HOLD outcomes < 30 after 30 days (insufficient data)
- Pipeline experienced significant downtime (coverage < 70% on average)

The hypothesis **FAILS** if:
- Volatile HOLD ratio ≥ main HOLD ratio AND accuracy ≤ 45%
- No meaningful regime diversity after 30 days

---

## Alert Interpretation Guide

| Alert | During Bootstrap | After Bootstrap |
|---|---|---|
| `OutcomePipelineBootstrapActive` | INFO (expected) | Should clear once >50 outcomes |
| `OutcomePipelineStalled` | Suppressed | Investigate immediately |
| `OutcomeJobNotRunning` | Suppressed | Critical — check scheduler |
| `DatasetStillBootstrapping` | Expected | Triggers at 72h if still BOOTSTRAP |
| `DatasetMaturityUseful` | N/A | Good signal — analysis now valid |
| `DatasetCalibrationReady` | N/A | Proceed with calibration reports |
| `CryptoDatasetLowIntegrity` | May fire day 0 | Investigate collector |
| `VolatileMonoRegimePersistent` | Suppressed | Fires from poupi-crypto-volatile |

---

## Grafana Dashboard Navigation

| Dashboard | Purpose |
|---|---|
| `outcome_pipeline.json` | Pipeline health, pending backlog, accuracy, MFE/MAE |
| `dataset_quality_crypto.json` | Freshness, coverage, integrity scores |
| `volatile_comparison.json` | Volatile vs main: signals, confidence, regimes |
| `crypto_quant_executive.json` | Executive summary: decisions, confidence, regime |
| `crypto_runtime_burnin.json` | Runtime burn-in health (first 7 days) |

---

## Recovery Runbook

### Scenario: outcomes stopped being evaluated

```bash
# 1. Check health endpoint
GET /api/v1/trading/validation/health
→ check last_evaluated_at, pending_count, stuck_count

# 2. Manual trigger
POST /api/v1/trading/validation/run-outcome-tracker?limit=500

# 3. Check if signals exist
GET /api/v1/trading/validation/signal-outcomes?limit=5

# 4. Check candle coverage
GET /api/v1/data-quality/run?module=trading
```

### Scenario: coverage drops below 50%

```bash
# Check collector
GET /api/v1/pipeline/status?domain=crypto

# Check normalized candles
GET /api/v1/data-quality/run?module=trading

# Check freshness  
GET /api/v1/trading/validation/signal-drift
→ check recent_total: should be > 0
```

### Scenario: bootstrap phase never clears

Investigate:
1. Is `signal_outcomes_job` running? Check Grafana `outcome_pipeline.json`
2. Are BUY/SELL signals being generated? May need 3+ days of candle history.
3. Is the volatile DB being used? Check `DATABASE_URL` env var in container.
4. Is normalization pipeline processing crypto candles? Check `candle_coverage_pct`.

---

*Last updated: 2026-05-26. Review at day 7, 14, and 30.*

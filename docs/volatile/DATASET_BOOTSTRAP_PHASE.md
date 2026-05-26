# Dataset Bootstrap Phase — Days 0-7

## Why Everything is HOLD Right Now

During the first 7 days of operation, it is **expected and correct** for nearly all trading
signals to be `HOLD`. This is not a bug or a misconfiguration.

The `TradingAnalyticsProcessor` uses the following indicators to generate BUY/SELL signals:

| Indicator | Why it produces HOLD early |
|-----------|---------------------------|
| **RSI** | Requires N candles of history to compute a meaningful divergence. With < 14 candles, RSI is undefined or flat. |
| **Moving averages** (fast/slow) | Fast/slow crossover requires enough history for both windows. A 20-period MA needs 20 candles before the first meaningful value. |
| **ADX** | Trend strength measurement requires a full 14-period window to detect trends. |
| **Breakout score** | Compares current price to recent range — undefined before a baseline range exists. |
| **Volume ratio** | Average volume is undefined without a history window. |

Until all indicators produce valid values, the processor defaults to `HOLD` (no strong
directional signal). This is the safest conservative behavior.

---

## Expected Timeline

| Timeframe | Candles needed | Hours until BUY/SELL possible |
|-----------|----------------|-------------------------------|
| `15m` | ~30 | ~7.5 hours |
| `1h` | ~30 | ~30 hours |

After the first 30+ candles exist per pair/timeframe, BUY/SELL signals **may** start
appearing — but only when market conditions actually trigger them (e.g., RSI oversold
+ ADX trending + volume spike).

**Note:** Even after indicators initialize, most signals will still be `HOLD` under neutral
market conditions. A 15-20% non-HOLD rate over 14 days is considered healthy.

---

## How to Tell if Bootstrap is Progressing Normally

### 1. Check candle count

```sql
SELECT symbol, timeframe, COUNT(*) as candles, MAX(timestamp) as latest
FROM normalized_market_candles
WHERE symbol IN ('SOL/USDT', 'DOGE/USDT', 'XRP/USDT')
GROUP BY symbol, timeframe
ORDER BY symbol, timeframe;
```

**Expected after 24h:** 96 rows per 15m pair, 24 per 1h pair (per symbol)

### 2. Check analytics rows

```sql
SELECT symbol, timeframe, signal, COUNT(*) as count
FROM trading_analytics
WHERE symbol IN ('SOL/USDT', 'DOGE/USDT', 'XRP/USDT')
  AND calculated_at > NOW() - INTERVAL '24 hours'
GROUP BY symbol, timeframe, signal
ORDER BY symbol, timeframe, signal;
```

**Expected after 24h:** All HOLD, count growing with each analytics_job cycle

### 3. Check dataset quality scores

```
GET /api/v1/data-quality/runs?module=crypto
```

Or view the **Dataset Quality** Grafana dashboard.

**Expected:** `dataset_integrity_score` ≥ 60 once 24h of data exists.

---

## What Indicates a Problem (Not Normal Bootstrap)

These patterns suggest a real pipeline failure, not normal bootstrap:

| Symptom | Likely cause |
|---------|--------------|
| Zero candles after 2+ hours | `collect_raw_job` or `normalize_job` not running |
| `dataset_integrity_score` < 20 after 48h | Collection gap or DB connection issue |
| `analytics_job` not creating rows | `TradingAnalyticsProcessor` throwing errors |
| All candles have identical timestamps | Exchange API returning cached/stale data |

Check: `GET /health`, scheduler logs, `signal_outcomes_job` and `dataset_quality_crypto_job` logs.

---

## Regresssion Detection

During bootstrap, the following patterns indicate regression:

1. **Coverage drops below 50%** on a pair that previously had 100% → collector issue
2. **Staleness spike** (`stale_candle_total` increases) → collection gap
3. **`analytics_job` errors** → check `DataQualityService` OHLC consistency results
4. **`signal_outcomes_job` errors** → check `TradingSignalOutcome` model and DB schema

The Prometheus alert `CryptoDatasetLowCoverage` (< 50% for 30min) will fire if coverage drops.

---

## When is the Dataset "Mature"?

The dataset is considered mature when:
- ≥ 7 days of continuous data exists per pair/timeframe
- Confidence calibration shows ≥ 10 evaluated outcomes per decile
- Signal drift analysis has enough historical baseline (≥ 100 rows per signal type)
- `dataset_integrity_score` ≥ 70 consistently for 48h+

Expected maturity date: **2026-06-02** (day 7 from runtime start).

# Volatile Runtime — State Audit T=0 (2026-05-26)

## Summary

Runtime `poupi-crypto-volatile` reached **operational state 3/3 pairs** on 2026-05-26 following the
fix of `DEFAULT_SYMBOLS` in `collectors/crypto/crypto_coin_ohlcv.py` (commits c3783e6 + a2f63a4).

This document records the T=0 baseline so future audits can measure drift.

---

## Container Health (at audit time)

| Container | Status | Image | Uptime |
|-----------|--------|-------|--------|
| `poupi-crypto-volatile-api-1` | healthy | data-core (baked) | ~hours |
| `poupi-crypto-volatile-scheduler-1` | healthy | data-core (baked) | ~hours |
| `poupi-crypto-volatile-worker-1` | healthy | data-core (baked) | ~hours |

All three containers passed `/live` and `/ready` probes.

## Active Pairs

```
DEFAULT_SYMBOLS = SOL/USDT, DOGE/USDT, XRP/USDT
SYMBOLS_TIMEFRAMES = 15m,1h
```

## Decisions/Cycle at T=0

- `collect_raw_job` (`crypto.crypto_coin_ohlcv`): every 15 min → 3 symbols × 2 TF = 6 pairs
- `normalize_job`: every 15 min → `CryptoSnapshotNormalizer`
- `analytics_job`: every 60 min → `TradingAnalyticsProcessor`
- Signal output: all `HOLD` (expected during bootstrap phase — see `DATASET_BOOTSTRAP_PHASE.md`)

## Data Pipeline Status

| Stage | Status |
|-------|--------|
| Raw collection (OHLCV) | ✅ Active — 6 pairs collecting |
| Normalization | ✅ Active — `normalized_market_candles` populated |
| Analytics | ✅ Active — `trading_analytics` rows being created |
| Signal outcomes | ✅ Active — `signal_outcomes_job` runs hourly |
| Dataset quality | ✅ Active — `dataset_quality_crypto_job` runs every 30 min |

## Hypothesis Being Validated

> SOL/DOGE/XRP generate richer regime diversity and better confidence calibration than BTC/ETH
> under the same analytical pipeline.

The hypothesis window is **30 days** (2026-05-26 → 2026-06-25).

## Memory / Resource Baseline

- Container memory limit: 512 MiB per service
- Observed usage at T=0: under 70% (safe)
- Swap pressure: none detected
- Redis: no distributed locking required (scheduler runs single-instance)

## Known Limitations at T=0

1. All signals are `HOLD` — this is expected; see bootstrap phase doc.
2. No outcome data yet — `signal_outcomes_job` will begin producing rows after ~6h.
3. Dataset quality scores are initializing — first meaningful scores after 24h of data.
4. Confidence calibration requires evaluated outcomes — meaningful after 72h minimum.

## Root Cause of Prior Failure (Resolved)

The volatile runtime was previously missing DOGE and XRP data because `DEFAULT_SYMBOLS` in
`collectors/crypto/crypto_coin_ohlcv.py` only contained `BTC/USDT` and `ETH/USDT`. The volatile
`SYMBOLS` env override was empty, causing the collector to use the (incomplete) default list.

**Fix**: Updated `DEFAULT_SYMBOLS` to include all 5 intended pairs. Volatile env uses
`SYMBOLS=SOL/USDT,DOGE/USDT,XRP/USDT` to select its 3 experimental pairs.

---

## Infrastructure Added at T=0 (this session)

| Component | Description |
|-----------|-------------|
| `collectors/crypto/validators.py` | Symbol validation + structured startup logging |
| `app/data_quality/crypto/` | Freshness, coverage, integrity scorer, DB model |
| `app/modules/trading/validation/` | Signal outcomes, calibration, drift, REST API |
| `app/modules/trading/interfaces/` | AI/Quant contracts (no runtime dependencies) |
| `alembic/versions/0019_*` | `crypto_dataset_quality_scores` table |
| `alembic/versions/0020_*` | `trading_signal_outcomes` table |
| `scheduler/jobs.py` | `dataset_quality_crypto_job` + `signal_outcomes_job` |
| `api/metrics.py` | 4 new Prometheus metrics: integrity/coverage/stale/gap |
| `prometheus/rules/dataset_quality_alerts.yml` | 6 alert rules |
| `grafana/dashboards/dataset_quality_crypto.json` | Dataset quality dashboard |
| `grafana/dashboards/volatile_comparison.json` | Volatile vs Main comparison |

## Next Checkpoint

See `VOLATILE_RUNTIME_VALIDATION.md` for acceptance criteria at day 14 (2026-06-09).

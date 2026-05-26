# Volatile Runtime — Day-14 Validation Criteria

**Evaluation date:** 2026-06-09 (14 days after runtime start on 2026-05-26)

This document defines the acceptance criteria for declaring the volatile runtime hypothesis
**validated**, **inconclusive**, or **failed** at the day-14 checkpoint.

---

## Hypothesis Recap

> SOL/DOGE/XRP generate richer regime diversity and better confidence calibration than
> BTC/ETH under the same analytical pipeline, making the volatile dataset more valuable
> for future ML training.

---

## Validation Criteria

### 1. Acceptance Rate (`acceptance_rate`)

**Metric:** `trading_signal_total{symbol=~"SOL.*|DOGE.*|XRP.*", signal!="HOLD"}` over 14 days

**Threshold:** ≥ 15% of signals must be non-HOLD (BUY or SELL)

| Result | Condition |
|--------|-----------|
| ✅ Pass | acceptance_rate ≥ 15% |
| ⚠️ Inconclusive | 5% ≤ acceptance_rate < 15% |
| ❌ Fail | acceptance_rate < 5% |

**How to evaluate:**
```promql
sum(trading_signal_total{symbol=~"SOL.*|DOGE.*|XRP.*", signal!="HOLD"})
/ sum(trading_signal_total{symbol=~"SOL.*|DOGE.*|XRP.*"})
```

---

### 2. Regime Entropy (`regime_entropy`)

**Metric:** Shannon entropy of `trading_regime_total` distribution across regime labels

**Threshold:** entropy > 0.5 (some regime diversity exists)

| Result | Condition |
|--------|-----------|
| ✅ Pass | entropy > 0.5 |
| ⚠️ Inconclusive | 0.2 < entropy ≤ 0.5 |
| ❌ Fail | entropy ≤ 0.2 (single regime dominates) |

**How to evaluate:** Compare regime distribution via Grafana `Volatile Comparison` dashboard
or query `trading_regime_total` labels for SOL/DOGE/XRP.

---

### 3. Confidence Dispersion (`confidence_dispersion`)

**Metric:** Standard deviation of `confidence` values across evaluated `trading_analytics` rows

**Threshold:** stddev ≥ 10 (meaningful spread across 0-100 range)

**How to evaluate:**
```sql
SELECT symbol, STDDEV(confidence) AS stddev_conf
FROM trading_analytics
WHERE symbol IN ('SOL/USDT', 'DOGE/USDT', 'XRP/USDT')
  AND calculated_at > NOW() - INTERVAL '14 days'
GROUP BY symbol;
```

---

### 4. Closed Outcome Ratio (`closed_outcome_ratio`)

**Metric:** `trading_signal_outcomes` rows / `trading_analytics` BUY+SELL rows

**Threshold:** ≥ 40% of BUY/SELL signals have an evaluated outcome

| Result | Condition |
|--------|-----------|
| ✅ Pass | closed_outcome_ratio ≥ 40% |
| ⚠️ Inconclusive | 20% ≤ ratio < 40% |
| ❌ Fail | ratio < 20% (outcome tracker may be malfunctioning) |

**How to evaluate:**
```sql
SELECT
  a.symbol,
  COUNT(DISTINCT o.analytics_id)::float / NULLIF(COUNT(DISTINCT a.id), 0) AS closed_ratio
FROM trading_analytics a
LEFT JOIN trading_signal_outcomes o ON o.analytics_id = a.id
WHERE a.signal IN ('BUY', 'SELL')
  AND a.calculated_at > NOW() - INTERVAL '14 days'
GROUP BY a.symbol;
```

---

### 5. Dataset Integrity Score (`dataset_integrity_score`)

**Metric:** `dataset_integrity_score` Prometheus gauge (Grafana: Dataset Quality dashboard)

**Threshold:** All three volatile pairs maintain score ≥ 60 over the 14-day window

| Result | Condition |
|--------|-----------|
| ✅ Pass | All pairs: avg(integrity_score) ≥ 60 |
| ⚠️ Inconclusive | Any pair: 40 ≤ avg < 60 |
| ❌ Fail | Any pair: avg < 40 (collection pipeline unreliable) |

---

## Overall Verdict

| Criteria Passed | Verdict |
|-----------------|---------|
| 5/5 | ✅ **Hypothesis Validated** — proceed with volatile pairs in main runtime |
| 3-4/5 | ⚠️ **Inconclusive** — extend window by 14 days |
| ≤ 2/5 | ❌ **Hypothesis Failed** — retire volatile runtime, retain infra |

---

## Actions by Verdict

### If Validated
- Promote SOL/DOGE/XRP into `DEFAULT_SYMBOLS` of the main runtime
- Archive volatile runtime (keep infra for next hypothesis)
- Tag commit as `hypothesis/volatile-3pairs/validated`

### If Inconclusive
- Extend window to day 28 (2026-06-23)
- Review which criteria are closest to passing
- Document blocking factor in `RUNTIME_STATE_*.md`

### If Failed
- Determine root cause (regime lock-in? calibration identical to BTC/ETH? data quality?)
- Retain infrastructure for next experiment
- Tag commit as `hypothesis/volatile-3pairs/failed`
- Archive this document with findings appended

---

## Dashboard References

- **Volatile Comparison:** `grafana/dashboards/volatile_comparison.json` (uid: `volatile-comparison`)
- **Dataset Quality:** `grafana/dashboards/dataset_quality_crypto.json` (uid: `dataset-quality-crypto`)
- **Signal Outcomes API:** `GET /api/v1/trading/validation/signal-outcomes`
- **Calibration API:** `GET /api/v1/trading/validation/calibration`
- **Signal Drift API:** `GET /api/v1/trading/validation/signal-drift`

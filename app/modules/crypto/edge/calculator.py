"""Edge outcome calculator — multi-horizon evaluation and quant metrics.

Phase 7 additions:
  - 14-day (336h) horizon
  - Regime Intelligence: per-regime metrics across all horizons
  - Confidence Intelligence: per-bucket metrics
  - Shadow Strategy: regime=UNKNOWN AND confidence>=75 (observation-only)
  - build_phase7_report(): full expansion report
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.analytics.models import TradingAnalytics
from app.modules.crypto.edge.models import SignalEdgeOutcome
from app.normalization.models import NormalizedMarketCandle

logger = logging.getLogger(__name__)

HORIZONS_HOURS: list[int] = [24, 72, 168, 336]

_TIMEFRAME_DELTA: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}

CONFIDENCE_BUCKETS: list[tuple[str, int, int]] = [
    ("55-64", 55, 64),
    ("65-74", 65, 74),
    ("75-84", 75, 84),
    ("85+", 85, 100),
]

# Shadow strategy filter constants (observation-only, no trading)
SHADOW_REGIME: str = "UNKNOWN"
SHADOW_MIN_CONFIDENCE: int = 75


def _candles_for_horizon(timeframe: str, horizon_hours: int) -> int:
    delta = _TIMEFRAME_DELTA.get(timeframe, timedelta(hours=1))
    return max(1, int(horizon_hours * 60 / (delta.total_seconds() / 60)))


def _bucket_label(confidence: int | None) -> str | None:
    if confidence is None:
        return None
    for label, low, high in CONFIDENCE_BUCKETS:
        if low <= confidence <= high:
            return label
    return None


def compute_edge_outcome(
    db: Session,
    analytics: TradingAnalytics,
    horizon_hours: int,
    now: datetime,
) -> SignalEdgeOutcome | None:
    timeframe = analytics.timeframe or "1h"
    calc_at = analytics.calculated_at
    if calc_at.tzinfo is None:
        calc_at = calc_at.replace(tzinfo=timezone.utc)
    horizon_end_ts = calc_at + timedelta(hours=horizon_hours)
    if horizon_end_ts > now:
        return None
    n_candles = _candles_for_horizon(timeframe, horizon_hours)
    candles = (
        db.query(NormalizedMarketCandle)
        .filter(
            NormalizedMarketCandle.symbol == analytics.symbol,
            NormalizedMarketCandle.timeframe == timeframe,
            NormalizedMarketCandle.timestamp > calc_at,
            NormalizedMarketCandle.timestamp <= horizon_end_ts,
        )
        .order_by(NormalizedMarketCandle.timestamp)
        .limit(n_candles + 5)
        .all()
    )
    if not candles:
        return None
    signal_price: float | None = None
    if analytics.market_candle_id:
        candle_row = db.get(NormalizedMarketCandle, analytics.market_candle_id)
        if candle_row and candle_row.close is not None:
            signal_price = float(candle_row.close)
    if signal_price is None and candles:
        signal_price = float(candles[0].close) if candles[0].close else None
    if signal_price is None or signal_price == 0:
        return None
    outcome_candle = candles[-1]
    outcome_price = float(outcome_candle.close) if outcome_candle.close else None
    if outcome_price is None:
        return None
    price_change_pct = (outcome_price - signal_price) / signal_price * 100
    highs = [float(c.high) for c in candles if c.high is not None]
    lows = [float(c.low) for c in candles if c.low is not None]
    if analytics.signal == "BUY":
        mfe_pct = (max(highs) - signal_price) / signal_price * 100 if highs else None
        mae_pct = (min(lows) - signal_price) / signal_price * 100 if lows else None
        outcome_correct = price_change_pct > 0
    else:
        mfe_pct = (signal_price - min(lows)) / signal_price * 100 if lows else None
        mae_pct = (signal_price - max(highs)) / signal_price * 100 if highs else None
        outcome_correct = price_change_pct < 0
    outcome_at = outcome_candle.timestamp
    if outcome_at is not None and outcome_at.tzinfo is None:
        outcome_at = outcome_at.replace(tzinfo=timezone.utc)
    return SignalEdgeOutcome(
        analytics_id=analytics.id,
        horizon_hours=horizon_hours,
        symbol=analytics.symbol,
        timeframe=timeframe,
        signal=analytics.signal,
        confidence=analytics.confidence,
        regime=analytics.regime,
        signal_at=calc_at,
        signal_price=round(signal_price, 8),
        outcome_at=outcome_at,
        outcome_price=round(outcome_price, 8),
        candles_elapsed=len(candles),
        price_change_pct=round(price_change_pct, 4),
        mfe_pct=round(mfe_pct, 4) if mfe_pct is not None else None,
        mae_pct=round(mae_pct, 4) if mae_pct is not None else None,
        outcome_correct=outcome_correct,
    )


class EdgeOutcomeTracker:
    """Idempotent batch tracker — evaluates pending BUY signals at all horizons."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def run(self, limit: int = 500, signal_filter: str = "BUY") -> dict[str, Any]:
        now = datetime.now(tz=timezone.utc)
        signal_types = [signal_filter] if signal_filter else ["BUY", "SELL"]
        candidates = (
            self.db.query(TradingAnalytics)
            .filter(TradingAnalytics.signal.in_(signal_types))
            .order_by(TradingAnalytics.calculated_at)
            .limit(limit * 3)
            .all()
        )
        computed = 0
        skipped = 0
        errors = 0
        for analytics in candidates:
            if computed >= limit:
                break
            for horizon_hours in HORIZONS_HOURS:
                already = (
                    self.db.query(SignalEdgeOutcome)
                    .filter(
                        SignalEdgeOutcome.analytics_id == analytics.id,
                        SignalEdgeOutcome.horizon_hours == horizon_hours,
                    )
                    .first()
                )
                if already:
                    continue
                try:
                    outcome = compute_edge_outcome(self.db, analytics, horizon_hours, now)
                    if outcome is None:
                        skipped += 1
                        continue
                    self.db.add(outcome)
                    computed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Edge outcome computation failed",
                        extra={"analytics_id": str(analytics.id), "error": str(exc)},
                    )
                    errors += 1
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        logger.info(
            "Edge outcome tracker completed",
            extra={"computed": computed, "skipped": skipped, "errors": errors},
        )
        return {"computed": computed, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Quant metric helpers
# ---------------------------------------------------------------------------


def _safe_div(num: float, den: float) -> float | None:
    return round(num / den, 4) if den != 0 else None


def _sharpe(returns: list[float]) -> float | None:
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    return round(mean / std, 4) if std != 0 else None


def _max_drawdown(returns: list[float]) -> float:
    if not returns:
        return 0.0
    peak = cumulative = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        dd = cumulative - peak
        if dd < max_dd:
            max_dd = dd
    return round(abs(max_dd), 4)


def _compute_group_metrics(outcomes: list[SignalEdgeOutcome]) -> dict[str, Any]:
    evaluated = [o for o in outcomes if o.outcome_correct is not None]
    returns = [float(o.price_change_pct) for o in evaluated if o.price_change_pct is not None]
    n = len(returns)
    if n == 0:
        return {
            "n_signals": len(outcomes),
            "n_evaluated": 0,
            "win_rate": None,
            "avg_return_pct": None,
            "expectancy": None,
            "profit_factor": None,
            "sharpe_ratio": None,
            "max_drawdown_pct": None,
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
        }
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = len(wins) / n
    avg_return = sum(returns) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    pf = _safe_div(sum(wins), abs(sum(losses))) if wins else None
    mfe_vals = [float(o.mfe_pct) for o in evaluated if o.mfe_pct is not None]
    mae_vals = [float(o.mae_pct) for o in evaluated if o.mae_pct is not None]
    return {
        "n_signals": len(outcomes),
        "n_evaluated": n,
        "win_rate": round(win_rate, 4),
        "avg_return_pct": round(avg_return, 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": pf,
        "sharpe_ratio": _sharpe(returns),
        "max_drawdown_pct": _max_drawdown(returns),
        "avg_mfe_pct": round(sum(mfe_vals) / len(mfe_vals), 4) if mfe_vals else None,
        "avg_mae_pct": round(sum(mae_vals) / len(mae_vals), 4) if mae_vals else None,
    }


def _confidence_calibration(outcomes: list[SignalEdgeOutcome]) -> dict[str, Any]:
    buckets: dict[str, list[SignalEdgeOutcome]] = {
        label: [] for label, _, _ in CONFIDENCE_BUCKETS
    }
    for o in outcomes:
        label = _bucket_label(o.confidence)
        if label and label in buckets:
            buckets[label].append(o)
    result: dict[str, Any] = {}
    for label, rows in buckets.items():
        evaluated = [r for r in rows if r.outcome_correct is not None]
        if not evaluated:
            result[label] = {"n": 0}
            continue
        wins = [r for r in evaluated if r.outcome_correct]
        returns = [float(r.price_change_pct) for r in evaluated if r.price_change_pct is not None]
        gp = sum(r for r in returns if r > 0)
        gl = abs(sum(r for r in returns if r <= 0))
        result[label] = {
            "n": len(evaluated),
            "win_rate": round(len(wins) / len(evaluated), 4) if evaluated else None,
            "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "profit_factor": _safe_div(gp, gl),
        }
    return result


def _assess_edge(outcomes: list[SignalEdgeOutcome], n_evaluated_24h: int) -> dict[str, Any]:
    if n_evaluated_24h < 5:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "reasoning": (
                f"Only {n_evaluated_24h} evaluated outcomes at 24h horizon. Need >=5."
            ),
            "minimum_required": 5,
            "available": n_evaluated_24h,
        }
    h24 = [o for o in outcomes if o.horizon_hours == 24 and o.outcome_correct is not None]
    wins_24h = sum(1 for o in h24 if o.outcome_correct)
    wr_24h = wins_24h / len(h24) if h24 else 0
    returns_24h = [float(o.price_change_pct) for o in h24 if o.price_change_pct is not None]
    avg_return = sum(returns_24h) / len(returns_24h) if returns_24h else 0
    wins_r = [r for r in returns_24h if r > 0]
    losses_r = [r for r in returns_24h if r <= 0]
    pf: float | None = None
    if losses_r and sum(losses_r) != 0:
        pf = sum(wins_r) / abs(sum(losses_r))
    if wr_24h > 0.55 and avg_return > 0:
        verdict = "EDGE_DETECTED"
    elif wr_24h < 0.45 or avg_return < 0:
        verdict = "NO_EDGE"
    else:
        verdict = "INCONCLUSIVE"
    parts = [
        f"24h win_rate={wr_24h:.1%} ({wins_24h}/{len(h24)})",
        f"avg_return={avg_return:.2f}%",
        f"profit_factor={pf:.2f}" if pf is not None else "profit_factor=N/A (all wins)",
    ]
    return {
        "verdict": verdict,
        "reasoning": " | ".join(parts),
        "win_rate_24h": round(wr_24h, 4),
        "avg_return_24h_pct": round(avg_return, 4),
        "profit_factor_24h": round(pf, 4) if pf is not None else None,
        "n_evaluated_24h": len(h24),
    }


# ---------------------------------------------------------------------------
# Phase 6 backward-compat report
# ---------------------------------------------------------------------------


def build_edge_report(
    db: Session,
    horizon_hours: int | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    analytics_q = db.query(TradingAnalytics).filter(TradingAnalytics.signal == "BUY")
    if symbol:
        analytics_q = analytics_q.filter(TradingAnalytics.symbol == symbol)
    if timeframe:
        analytics_q = analytics_q.filter(TradingAnalytics.timeframe == timeframe)
    all_analytics = analytics_q.order_by(TradingAnalytics.calculated_at).all()

    outcomes_q = db.query(SignalEdgeOutcome).filter(SignalEdgeOutcome.signal == "BUY")
    if symbol:
        outcomes_q = outcomes_q.filter(SignalEdgeOutcome.symbol == symbol)
    if timeframe:
        outcomes_q = outcomes_q.filter(SignalEdgeOutcome.timeframe == timeframe)
    if horizon_hours:
        outcomes_q = outcomes_q.filter(SignalEdgeOutcome.horizon_hours == horizon_hours)
    all_outcomes = outcomes_q.all()

    outcome_index: dict[tuple[uuid.UUID, int], SignalEdgeOutcome] = {}
    for o in all_outcomes:
        if o.analytics_id:
            outcome_index[(o.analytics_id, o.horizon_hours)] = o

    candle_ids = [a.market_candle_id for a in all_analytics if a.market_candle_id]
    candle_map: dict[uuid.UUID, NormalizedMarketCandle] = {}
    if candle_ids:
        candle_rows = (
            db.query(NormalizedMarketCandle)
            .filter(NormalizedMarketCandle.id.in_(candle_ids))
            .all()
        )
        candle_map = {c.id: c for c in candle_rows}

    horizons_to_show = [horizon_hours] if horizon_hours else HORIZONS_HOURS
    edge_registry = []
    for a in all_analytics:
        candle = candle_map.get(a.market_candle_id) if a.market_candle_id else None
        entry: dict[str, Any] = {
            "analytics_id": str(a.id),
            "symbol": a.symbol,
            "timeframe": a.timeframe,
            "signal_at": a.calculated_at.isoformat() if a.calculated_at else None,
            "signal_price": float(candle.close) if candle and candle.close else None,
            "confidence": a.confidence,
            "confidence_bucket": _bucket_label(a.confidence),
            "regime": a.regime,
            "indicators": {
                "rsi": float(a.rsi) if a.rsi else None,
                "adx": float(a.adx) if a.adx else None,
                "volume_ratio": float(a.volume_ratio) if a.volume_ratio else None,
                "breakout_score": float(a.breakout_score) if a.breakout_score else None,
            },
            "outcomes": {},
        }
        for h in horizons_to_show:
            o = outcome_index.get((a.id, h))
            if o:
                entry["outcomes"][f"{h}h"] = {
                    "outcome_at": o.outcome_at.isoformat() if o.outcome_at else None,
                    "price_change_pct": float(o.price_change_pct) if o.price_change_pct else None,
                    "mfe_pct": float(o.mfe_pct) if o.mfe_pct else None,
                    "mae_pct": float(o.mae_pct) if o.mae_pct else None,
                    "outcome_correct": o.outcome_correct,
                }
            else:
                entry["outcomes"][f"{h}h"] = None
        edge_registry.append(entry)

    calibration: dict[str, Any] = {}
    for h in horizons_to_show:
        h_outcomes = [o for o in all_outcomes if o.horizon_hours == h]
        calibration[f"{h}h"] = _confidence_calibration(h_outcomes)

    def _filt(
        f_sym: str | None,
        f_tf: str | None,
        f_reg: str | None,
        f_bkt: str | None,
        h: int,
    ) -> list[SignalEdgeOutcome]:
        return [
            o
            for o in all_outcomes
            if (f_sym is None or o.symbol == f_sym)
            and (f_tf is None or o.timeframe == f_tf)
            and (f_reg is None or o.regime == f_reg)
            and (f_bkt is None or _bucket_label(o.confidence) == f_bkt)
            and o.horizon_hours == h
        ]

    qm: dict[str, Any] = {}
    for h in horizons_to_show:
        qm[f"overall_{h}h"] = _compute_group_metrics(
            [o for o in all_outcomes if o.horizon_hours == h]
        )

    symbols_seen = sorted({a.symbol for a in all_analytics})
    timeframes_seen = sorted({a.timeframe for a in all_analytics})
    regimes_seen = sorted({a.regime for a in all_analytics if a.regime})

    qm["by_symbol"] = {
        sym: {
            f"{h}h": _compute_group_metrics(_filt(sym, None, None, None, h))
            for h in horizons_to_show
        }
        for sym in symbols_seen
    }
    qm["by_timeframe"] = {
        tf: {
            f"{h}h": _compute_group_metrics(_filt(None, tf, None, None, h))
            for h in horizons_to_show
        }
        for tf in timeframes_seen
    }
    qm["by_regime"] = {
        regime: {
            f"{h}h": _compute_group_metrics(_filt(None, None, regime, None, h))
            for h in horizons_to_show
        }
        for regime in regimes_seen
    }
    qm["by_confidence_bucket"] = {
        label: {
            f"{h}h": _compute_group_metrics(_filt(None, None, None, label, h))
            for h in horizons_to_show
        }
        for label, _, _ in CONFIDENCE_BUCKETS
    }

    n_eval_24 = sum(
        1 for o in all_outcomes if o.outcome_correct is not None and o.horizon_hours == 24
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_buy_signals": len(all_analytics),
            "total_outcomes_computed": len(all_outcomes),
            "horizons_available": sorted({o.horizon_hours for o in all_outcomes}),
            "symbols_covered": symbols_seen,
            "timeframes_covered": timeframes_seen,
        },
        "edge_registry": edge_registry,
        "confidence_calibration": calibration,
        "quant_metrics": qm,
        "go_no_go": _assess_edge(all_outcomes, n_eval_24),
    }


# ---------------------------------------------------------------------------
# Phase 7 — Expanded edge report
# ---------------------------------------------------------------------------


def _regime_intelligence(
    outcomes: list[SignalEdgeOutcome],
    horizons: list[int],
) -> dict[str, Any]:
    """Per-regime metrics across all horizons."""
    regimes = sorted({o.regime for o in outcomes if o.regime})
    result: dict[str, Any] = {}
    for regime in regimes:
        regime_outcomes = [o for o in outcomes if o.regime == regime]
        by_horizon: dict[str, Any] = {}
        for h in horizons:
            h_outcomes = [o for o in regime_outcomes if o.horizon_hours == h]
            by_horizon[f"{h}h"] = _compute_group_metrics(h_outcomes)
        result[regime] = {
            "total_signals": len({o.analytics_id for o in regime_outcomes}),
            "by_horizon": by_horizon,
        }
    return result


def _confidence_intelligence(
    outcomes: list[SignalEdgeOutcome],
    horizons: list[int],
) -> dict[str, Any]:
    """Per-confidence-bucket metrics across all horizons."""
    result: dict[str, Any] = {}
    for label, low, high in CONFIDENCE_BUCKETS:
        bucket_outcomes = [
            o for o in outcomes if o.confidence is not None and low <= o.confidence <= high
        ]
        by_horizon: dict[str, Any] = {}
        for h in horizons:
            h_outcomes = [o for o in bucket_outcomes if o.horizon_hours == h]
            by_horizon[f"{h}h"] = _compute_group_metrics(h_outcomes)
        result[label] = {
            "range": f"{low}-{high if high < 100 else '+'}",
            "total_signals": len({o.analytics_id for o in bucket_outcomes}),
            "by_horizon": by_horizon,
        }
    return result


def _shadow_strategy_metrics(
    all_outcomes: list[SignalEdgeOutcome],
    all_analytics: list[TradingAnalytics],
    horizons: list[int],
) -> dict[str, Any]:
    """Shadow strategy: regime=UNKNOWN AND confidence>=75.

    Observation-only — no trades, no strategy changes.
    Computes parallel metrics to validate whether this filter improves edge.
    """
    shadow_ids = {
        a.id
        for a in all_analytics
        if a.regime == SHADOW_REGIME
        and a.confidence is not None
        and a.confidence >= SHADOW_MIN_CONFIDENCE
    }
    shadow_outcomes = [o for o in all_outcomes if o.analytics_id in shadow_ids]
    current_outcomes = [o for o in all_outcomes if o.analytics_id not in shadow_ids]

    by_horizon: dict[str, Any] = {}
    current_by_horizon: dict[str, Any] = {}
    for h in horizons:
        shadow_h = [o for o in shadow_outcomes if o.horizon_hours == h]
        current_h = [o for o in current_outcomes if o.horizon_hours == h]
        by_horizon[f"{h}h"] = _compute_group_metrics(shadow_h)
        current_by_horizon[f"{h}h"] = _compute_group_metrics(current_h)

    return {
        "filter": f"regime={SHADOW_REGIME} AND confidence>={SHADOW_MIN_CONFIDENCE}",
        "n_shadow_signals": len(shadow_ids),
        "n_excluded_signals": len({a.id for a in all_analytics}) - len(shadow_ids),
        "shadow_metrics_by_horizon": by_horizon,
        "current_strategy_excl_shadow_by_horizon": current_by_horizon,
        "note": (
            "Shadow strategy is OBSERVATION ONLY. "
            "No trades executed. No strategy logic altered."
        ),
    }


def _best_segment(
    regime_intel: dict[str, Any],
    conf_intel: dict[str, Any],
    primary_horizon: str = "72h",
) -> tuple[str | None, str | None]:
    """Return (best_regime, best_bucket) based on profit_factor at primary_horizon."""
    best_regime: str | None = None
    best_regime_pf: float = -1.0
    for regime, data in regime_intel.items():
        pf = (data["by_horizon"].get(primary_horizon) or {}).get("profit_factor") or 0.0
        if pf > best_regime_pf:
            best_regime_pf = pf
            best_regime = regime

    best_bucket: str | None = None
    best_bucket_pf: float = -1.0
    for bucket, data in conf_intel.items():
        pf = (data["by_horizon"].get(primary_horizon) or {}).get("profit_factor") or 0.0
        if pf > best_bucket_pf:
            best_bucket_pf = pf
            best_bucket = bucket

    return best_regime, best_bucket


def _go_no_go_by_segment(
    regime_intel: dict[str, Any],
    conf_intel: dict[str, Any],
    shadow: dict[str, Any],
) -> dict[str, Any]:
    """GO/NO-GO verdict per segment at 72h horizon (most reliable in phase 6)."""

    def _verdict(metrics: dict[str, Any]) -> str:
        wr = metrics.get("win_rate")
        avg = metrics.get("avg_return_pct")
        n = metrics.get("n_evaluated", 0)
        if n < 3:
            return "INSUFFICIENT_DATA"
        if wr is None or avg is None:
            return "INSUFFICIENT_DATA"
        if wr > 0.55 and avg > 0:
            return "EDGE_DETECTED"
        if wr < 0.45 or avg < 0:
            return "NO_EDGE"
        return "INCONCLUSIVE"

    result: dict[str, Any] = {}
    for horizon_key in ["24h", "72h", "168h", "336h"]:
        result[f"regimes_{horizon_key}"] = {
            regime: _verdict(data["by_horizon"].get(horizon_key, {}))
            for regime, data in regime_intel.items()
        }
        result[f"buckets_{horizon_key}"] = {
            bucket: _verdict(data["by_horizon"].get(horizon_key, {}))
            for bucket, data in conf_intel.items()
        }
        shadow_m = shadow["shadow_metrics_by_horizon"].get(horizon_key, {})
        result[f"shadow_{horizon_key}"] = _verdict(shadow_m)

    return result


def build_phase7_report(
    db: Session,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """Phase 7 expanded edge report.

    Returns:
    - regime_intelligence: per-regime metrics across all 4 horizons
    - confidence_intelligence: per-bucket metrics across all 4 horizons
    - shadow_strategy: UNKNOWN + conf>=75 parallel metrics
    - go_no_go_by_segment: verdict per regime / bucket / shadow
    - best_regime, best_bucket
    - overall_verdict
    - outcomes: raw outcome rows
    - strategy_comparison: current vs shadow at each horizon
    """
    analytics_q = db.query(TradingAnalytics).filter(TradingAnalytics.signal == "BUY")
    if symbol:
        analytics_q = analytics_q.filter(TradingAnalytics.symbol == symbol)
    if timeframe:
        analytics_q = analytics_q.filter(TradingAnalytics.timeframe == timeframe)
    all_analytics = analytics_q.order_by(TradingAnalytics.calculated_at).all()

    outcomes_q = db.query(SignalEdgeOutcome).filter(SignalEdgeOutcome.signal == "BUY")
    if symbol:
        outcomes_q = outcomes_q.filter(SignalEdgeOutcome.symbol == symbol)
    if timeframe:
        outcomes_q = outcomes_q.filter(SignalEdgeOutcome.timeframe == timeframe)
    all_outcomes = outcomes_q.all()

    horizons_available = sorted({o.horizon_hours for o in all_outcomes})
    horizons = horizons_available or HORIZONS_HOURS

    regime_intel = _regime_intelligence(all_outcomes, horizons)
    conf_intel = _confidence_intelligence(all_outcomes, horizons)
    shadow = _shadow_strategy_metrics(all_outcomes, all_analytics, horizons)
    go_no_go = _go_no_go_by_segment(regime_intel, conf_intel, shadow)
    best_regime, best_bucket = _best_segment(regime_intel, conf_intel, primary_horizon="72h")

    # Overall verdict: use 72h as primary
    h72_all = [o for o in all_outcomes if o.horizon_hours == 72]
    overall_metrics = _compute_group_metrics(h72_all)
    n_eval_72 = overall_metrics.get("n_evaluated", 0)
    if n_eval_72 < 5:
        overall_verdict = "INSUFFICIENT_DATA"
    elif (overall_metrics.get("win_rate") or 0) > 0.55 and (
        overall_metrics.get("avg_return_pct") or 0
    ) > 0:
        overall_verdict = "EDGE_DETECTED"
    elif (overall_metrics.get("win_rate") or 1) < 0.45 or (
        overall_metrics.get("avg_return_pct") or 1
    ) < 0:
        overall_verdict = "NO_EDGE"
    else:
        overall_verdict = "INCONCLUSIVE"

    # Raw outcomes for the response
    outcomes_serialized = [
        {
            "analytics_id": str(o.analytics_id) if o.analytics_id else None,
            "symbol": o.symbol,
            "timeframe": o.timeframe,
            "horizon_hours": o.horizon_hours,
            "signal_at": o.signal_at.isoformat() if o.signal_at else None,
            "regime": o.regime,
            "confidence": o.confidence,
            "confidence_bucket": _bucket_label(o.confidence),
            "price_change_pct": float(o.price_change_pct) if o.price_change_pct else None,
            "mfe_pct": float(o.mfe_pct) if o.mfe_pct else None,
            "mae_pct": float(o.mae_pct) if o.mae_pct else None,
            "outcome_correct": o.outcome_correct,
            "is_shadow": (
                o.regime == SHADOW_REGIME
                and o.confidence is not None
                and o.confidence >= SHADOW_MIN_CONFIDENCE
            ),
        }
        for o in sorted(all_outcomes, key=lambda x: (x.signal_at or datetime.min, x.horizon_hours))
    ]

    # Strategy comparison: current (all) vs shadow at each horizon
    strategy_comparison: dict[str, Any] = {}
    for h in horizons:
        all_h = [o for o in all_outcomes if o.horizon_hours == h]
        shadow_h = [
            o
            for o in all_h
            if o.regime == SHADOW_REGIME
            and o.confidence is not None
            and o.confidence >= SHADOW_MIN_CONFIDENCE
        ]
        strategy_comparison[f"{h}h"] = {
            "current_strategy": _compute_group_metrics(all_h),
            "shadow_strategy": _compute_group_metrics(shadow_h),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_buy_signals": len(all_analytics),
            "total_outcomes_computed": len(all_outcomes),
            "horizons_evaluated": horizons,
            "symbols_covered": sorted({a.symbol for a in all_analytics}),
            "timeframes_covered": sorted({a.timeframe for a in all_analytics}),
            "regimes_seen": sorted({a.regime for a in all_analytics if a.regime}),
            "shadow_filter": f"regime={SHADOW_REGIME} AND confidence>={SHADOW_MIN_CONFIDENCE}",
            "n_shadow_signals": shadow["n_shadow_signals"],
        },
        "regime_intelligence": regime_intel,
        "confidence_intelligence": conf_intel,
        "shadow_strategy": shadow,
        "strategy_comparison": strategy_comparison,
        "go_no_go_by_segment": go_no_go,
        "best_regime": best_regime,
        "best_bucket": best_bucket,
        "overall_verdict": overall_verdict,
        "overall_metrics_72h": overall_metrics,
        "outcomes": outcomes_serialized,
    }

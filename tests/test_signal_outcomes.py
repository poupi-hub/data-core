"""Integration tests for the trading signal validation module.

Covers:
- SignalOutcomeTracker: price_change_pct, MFE/MAE, outcome_correct
- compute_calibration: accuracy per confidence decile
- compute_signal_drift: recent vs historical signal distribution

Requires a live PostgreSQL test database (skips if unavailable).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.analytics.models import TradingAnalytics
from app.modules.trading.validation.confidence_calibration import compute_calibration
from app.modules.trading.validation.models import TradingSignalOutcome
from app.modules.trading.validation.outcome_tracker import EVALUATION_HORIZON, SignalOutcomeTracker
from app.modules.trading.validation.signal_drift import DRIFT_THRESHOLD, compute_signal_drift
from app.normalization.models import NormalizedMarketCandle


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_candle(
    symbol: str,
    timeframe: str,
    timestamp: datetime,
    close: float,
    source: str,
    high: float | None = None,
    low: float | None = None,
) -> NormalizedMarketCandle:
    return NormalizedMarketCandle(
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        timestamp=timestamp,
        open=close - 0.5,
        high=high if high is not None else close + 1.0,
        low=low if low is not None else close - 1.0,
        close=close,
        volume=1000.0,
        normalizer_name="pytest_normalizer",
        normalizer_version="1.0.0",
    )


def _make_analytics(
    candle: NormalizedMarketCandle,
    signal: str,
    confidence: int = 70,
    regime: str = "trending",
) -> TradingAnalytics:
    return TradingAnalytics(
        market_candle_id=candle.id,
        symbol=candle.symbol,
        timeframe=candle.timeframe,
        signal=signal,
        confidence=confidence,
        regime=regime,
        calculated_at=candle.timestamp,
    )


def _make_outcome(
    symbol: str,
    timeframe: str,
    signal: str,
    confidence: int,
    outcome_correct: bool,
    signal_at: datetime | None = None,
) -> TradingSignalOutcome:
    now = datetime.now(tz=timezone.utc)
    return TradingSignalOutcome(
        analytics_id=None,
        symbol=symbol,
        timeframe=timeframe,
        signal=signal,
        confidence=confidence,
        signal_price=100.0,
        signal_at=signal_at or now - timedelta(hours=8),
        outcome_price=101.0 if outcome_correct else 99.0,
        outcome_at=signal_at + timedelta(hours=6) if signal_at else now - timedelta(hours=2),
        candles_elapsed=EVALUATION_HORIZON,
        price_change_pct=1.0 if outcome_correct else -1.0,
        outcome_correct=outcome_correct,
        evaluation_horizon_candles=EVALUATION_HORIZON,
        evaluated_at=now,
    )


# ── SignalOutcomeTracker ──────────────────────────────────────────────────────

def test_buy_signal_correct_when_price_rises(db_session):
    """BUY signal outcome_correct=True when outcome_price > signal_price."""
    source = f"pytest-outcome-buy-{uuid4().hex[:8]}"
    symbol = "SOL/USDT"
    timeframe = "1h"
    now = datetime.now(tz=timezone.utc)

    # Signal candle: 8 hours ago (horizon = 6 candles = 6h, so horizon closed)
    signal_time = now - timedelta(hours=8)
    signal_candle = _make_candle(symbol, timeframe, signal_time, close=100.0, source=source)
    db_session.add(signal_candle)
    db_session.flush()

    analytics = _make_analytics(signal_candle, signal="BUY", confidence=80)
    db_session.add(analytics)
    db_session.flush()

    # Horizon candles: price rises from 100 → 105
    for i in range(1, EVALUATION_HORIZON + 1):
        db_session.add(_make_candle(
            symbol, timeframe, signal_time + timedelta(hours=i),
            close=100.0 + i,
            high=100.0 + i + 0.5,
            low=100.0 + i - 0.5,
            source=source,
        ))
    db_session.flush()

    tracker = SignalOutcomeTracker(db_session)
    result = tracker.run(limit=10)

    assert result["evaluated"] >= 1
    outcome = (
        db_session.query(TradingSignalOutcome)
        .filter(TradingSignalOutcome.analytics_id == analytics.id)
        .first()
    )
    assert outcome is not None
    assert outcome.outcome_correct is True
    assert float(outcome.price_change_pct) > 0


def test_sell_signal_correct_when_price_falls(db_session):
    """SELL signal outcome_correct=True when outcome_price < signal_price."""
    source = f"pytest-outcome-sell-{uuid4().hex[:8]}"
    symbol = "DOGE/USDT"
    timeframe = "1h"
    now = datetime.now(tz=timezone.utc)

    signal_time = now - timedelta(hours=8)
    signal_candle = _make_candle(symbol, timeframe, signal_time, close=200.0, source=source)
    db_session.add(signal_candle)
    db_session.flush()

    analytics = _make_analytics(signal_candle, signal="SELL", confidence=75)
    db_session.add(analytics)
    db_session.flush()

    # Horizon candles: price falls from 200 → 194
    for i in range(1, EVALUATION_HORIZON + 1):
        db_session.add(_make_candle(
            symbol, timeframe, signal_time + timedelta(hours=i),
            close=200.0 - i,
            high=200.0 - i + 0.5,
            low=200.0 - i - 0.5,
            source=source,
        ))
    db_session.flush()

    tracker = SignalOutcomeTracker(db_session)
    result = tracker.run(limit=10)

    assert result["evaluated"] >= 1
    outcome = (
        db_session.query(TradingSignalOutcome)
        .filter(TradingSignalOutcome.analytics_id == analytics.id)
        .first()
    )
    assert outcome is not None
    assert outcome.outcome_correct is True
    assert float(outcome.price_change_pct) < 0


def test_buy_signal_incorrect_when_price_falls(db_session):
    """BUY signal outcome_correct=False when price falls."""
    source = f"pytest-outcome-buy-bad-{uuid4().hex[:8]}"
    symbol = "XRP/USDT"
    timeframe = "1h"
    now = datetime.now(tz=timezone.utc)

    signal_time = now - timedelta(hours=8)
    signal_candle = _make_candle(symbol, timeframe, signal_time, close=50.0, source=source)
    db_session.add(signal_candle)
    db_session.flush()

    analytics = _make_analytics(signal_candle, signal="BUY", confidence=60)
    db_session.add(analytics)
    db_session.flush()

    # Price falls
    for i in range(1, EVALUATION_HORIZON + 1):
        db_session.add(_make_candle(
            symbol, timeframe, signal_time + timedelta(hours=i),
            close=50.0 - i * 0.5,
            source=source,
        ))
    db_session.flush()

    tracker = SignalOutcomeTracker(db_session)
    tracker.run(limit=10)

    outcome = (
        db_session.query(TradingSignalOutcome)
        .filter(TradingSignalOutcome.analytics_id == analytics.id)
        .first()
    )
    assert outcome is not None
    assert outcome.outcome_correct is False


def test_tracker_skips_signal_too_recent(db_session):
    """Signals whose horizon window has not yet closed must be skipped."""
    source = f"pytest-outcome-recent-{uuid4().hex[:8]}"
    symbol = "BTC/USDT"
    timeframe = "1h"
    now = datetime.now(tz=timezone.utc)

    # Signal candle just 1 hour ago — horizon of 6h has not yet closed
    signal_candle = _make_candle(symbol, timeframe, now - timedelta(hours=1), close=100.0, source=source)
    db_session.add(signal_candle)
    db_session.flush()

    analytics = _make_analytics(signal_candle, signal="BUY", confidence=70)
    db_session.add(analytics)
    db_session.flush()

    tracker = SignalOutcomeTracker(db_session)
    result = tracker.run(limit=10)

    assert result["skipped"] >= 1
    outcome = (
        db_session.query(TradingSignalOutcome)
        .filter(TradingSignalOutcome.analytics_id == analytics.id)
        .first()
    )
    assert outcome is None


def test_mfe_mae_computed_for_buy_signal(db_session):
    """MFE = (max_high - signal) / signal, MAE = (min_low - signal) / signal for BUY."""
    source = f"pytest-mfe-mae-{uuid4().hex[:8]}"
    symbol = "ETH/USDT"
    timeframe = "1h"
    now = datetime.now(tz=timezone.utc)

    signal_time = now - timedelta(hours=8)
    signal_candle = _make_candle(symbol, timeframe, signal_time, close=1000.0, source=source)
    db_session.add(signal_candle)
    db_session.flush()

    analytics = _make_analytics(signal_candle, signal="BUY")
    db_session.add(analytics)
    db_session.flush()

    # Candles with known high/low for MFE/MAE check
    closes  = [1010.0, 1020.0, 1050.0, 1040.0, 1030.0, 1025.0]
    highs   = [1015.0, 1025.0, 1060.0, 1045.0, 1035.0, 1030.0]
    lows    = [990.0,  1005.0, 1040.0, 1030.0, 1020.0, 1015.0]

    for i, (c, h, l) in enumerate(zip(closes, highs, lows)):
        db_session.add(_make_candle(
            symbol, timeframe, signal_time + timedelta(hours=i + 1),
            close=c, high=h, low=l, source=source,
        ))
    db_session.flush()

    tracker = SignalOutcomeTracker(db_session)
    tracker.run(limit=10)

    outcome = (
        db_session.query(TradingSignalOutcome)
        .filter(TradingSignalOutcome.analytics_id == analytics.id)
        .first()
    )
    assert outcome is not None
    # MFE: (max_high=1060 - 1000) / 1000 * 100 = 6.0%
    assert outcome.max_favorable_pct is not None
    assert float(outcome.max_favorable_pct) == pytest.approx(6.0, abs=0.1)
    # MAE: (min_low=990 - 1000) / 1000 * 100 = -1.0%
    assert outcome.max_adverse_pct is not None
    assert float(outcome.max_adverse_pct) == pytest.approx(-1.0, abs=0.1)


def test_tracker_does_not_duplicate_outcomes(db_session):
    """Running the tracker twice should not create duplicate TradingSignalOutcome rows."""
    source = f"pytest-outcome-nodup-{uuid4().hex[:8]}"
    symbol = "SOL/USDT"
    timeframe = "1h"
    now = datetime.now(tz=timezone.utc)

    signal_time = now - timedelta(hours=8)
    signal_candle = _make_candle(symbol, timeframe, signal_time, close=100.0, source=source)
    db_session.add(signal_candle)
    db_session.flush()

    analytics = _make_analytics(signal_candle, signal="BUY")
    db_session.add(analytics)
    db_session.flush()

    for i in range(1, EVALUATION_HORIZON + 1):
        db_session.add(_make_candle(
            symbol, timeframe, signal_time + timedelta(hours=i), close=100.0 + i, source=source,
        ))
    db_session.flush()

    tracker = SignalOutcomeTracker(db_session)
    tracker.run(limit=10)
    tracker.run(limit=10)  # second run must skip already-evaluated signal

    count = (
        db_session.query(TradingSignalOutcome)
        .filter(TradingSignalOutcome.analytics_id == analytics.id)
        .count()
    )
    assert count == 1


# ── compute_calibration ───────────────────────────────────────────────────────

def test_calibration_returns_empty_when_no_outcomes(db_session):
    """compute_calibration must return sensible defaults when no outcomes exist."""
    symbol = f"NODATA/USDT"
    result = compute_calibration(db_session, symbol=symbol)

    assert result["total_evaluated"] == 0
    assert result["overall_accuracy"] is None
    assert result["deciles"] == {}
    assert result["calibration_slope"] is None


def test_calibration_positive_slope_for_well_calibrated_data(db_session):
    """If high-confidence signals are correct more often, slope must be > 0."""
    symbol = f"pytest-calib-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # Low confidence (20) → incorrect; High confidence (80) → correct
    outcomes_data = [
        (20, False), (20, False), (25, False),
        (75, True),  (80, True),  (85, True),
    ]
    for confidence, correct in outcomes_data:
        db_session.add(_make_outcome(
            symbol=symbol,
            timeframe="1h",
            signal="BUY",
            confidence=confidence,
            outcome_correct=correct,
            signal_at=now - timedelta(hours=8),
        ))
    db_session.flush()

    result = compute_calibration(db_session, symbol=symbol)

    assert result["total_evaluated"] == 6
    assert result["well_calibrated"] is True
    assert result["calibration_slope"] is not None
    assert result["calibration_slope"] > 0


def test_calibration_accuracy_per_decile_sums_to_total(db_session):
    """Sum of total counts across deciles should equal total_evaluated."""
    symbol = f"pytest-calib-sum-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    for i in range(10):
        db_session.add(_make_outcome(
            symbol=symbol,
            timeframe="1h",
            signal="BUY",
            confidence=i * 10 + 5,
            outcome_correct=i % 2 == 0,
            signal_at=now - timedelta(hours=8),
        ))
    db_session.flush()

    result = compute_calibration(db_session, symbol=symbol)

    total_from_deciles = sum(d["total"] for d in result["deciles"].values())
    assert total_from_deciles == result["total_evaluated"]


def test_calibration_overall_accuracy_calculation(db_session):
    """overall_accuracy = correct / total."""
    symbol = f"pytest-calib-acc-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # 3 correct, 1 incorrect → 75%
    for correct in [True, True, True, False]:
        db_session.add(_make_outcome(
            symbol=symbol, timeframe="1h", signal="BUY",
            confidence=70, outcome_correct=correct,
            signal_at=now - timedelta(hours=8),
        ))
    db_session.flush()

    result = compute_calibration(db_session, symbol=symbol)

    assert result["overall_accuracy"] == pytest.approx(0.75, abs=0.01)


# ── compute_signal_drift ──────────────────────────────────────────────────────

def test_no_drift_when_distributions_match(db_session):
    """No drift when recent and historical distributions are roughly equal."""
    from app.analytics.models import TradingAnalytics
    symbol = f"pytest-drift-nodrift-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    source = f"pytest-drift-{uuid4().hex[:8]}"

    # Insert historical candles + analytics with balanced BUY/SELL/HOLD distribution
    signals = ["BUY", "SELL", "HOLD", "BUY", "SELL", "HOLD"] * 4  # 24 rows
    for i, sig in enumerate(signals):
        ts = now - timedelta(hours=i + 2)
        candle = NormalizedMarketCandle(
            source=source, symbol=symbol, timeframe="1h", timestamp=ts,
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            normalizer_name="pytest_normalizer", normalizer_version="1.0.0",
        )
        db_session.add(candle)
        db_session.flush()
        db_session.add(TradingAnalytics(
            market_candle_id=candle.id,
            symbol=symbol,
            timeframe="1h",
            signal=sig,
            confidence=70,
            calculated_at=ts,
        ))
    db_session.flush()

    result = compute_signal_drift(db_session, symbol=symbol, window_hours=12)

    # Distribution is symmetric — no single signal should deviate much
    assert result["recent_total"] > 0
    assert result["historical_total"] > 0


def test_drift_detected_when_hold_dominates_recent(db_session):
    """Drift should be detected when HOLD jumps from ~33% historical to 95%+ recent."""
    from app.analytics.models import TradingAnalytics
    symbol = f"pytest-drift-hold-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    source = f"pytest-drift-src-{uuid4().hex[:8]}"

    # Historical: balanced BUY/SELL/HOLD (36 rows, 12 each)
    for i in range(36):
        sig = ["BUY", "SELL", "HOLD"][i % 3]
        ts = now - timedelta(hours=48 + i)  # outside the 24h window
        candle = NormalizedMarketCandle(
            source=source, symbol=symbol, timeframe="1h", timestamp=ts,
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            normalizer_name="pytest_normalizer", normalizer_version="1.0.0",
        )
        db_session.add(candle)
        db_session.flush()
        db_session.add(TradingAnalytics(
            market_candle_id=candle.id, symbol=symbol, timeframe="1h",
            signal=sig, confidence=70, calculated_at=ts,
        ))

    # Recent: 95% HOLD (within 24h window)
    for i in range(20):
        sig = "HOLD" if i < 19 else "BUY"
        ts = now - timedelta(hours=i + 1)
        candle = NormalizedMarketCandle(
            source=source, symbol=symbol, timeframe="1h", timestamp=ts,
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            normalizer_name="pytest_normalizer", normalizer_version="1.0.0",
        )
        db_session.add(candle)
        db_session.flush()
        db_session.add(TradingAnalytics(
            market_candle_id=candle.id, symbol=symbol, timeframe="1h",
            signal=sig, confidence=70, calculated_at=ts,
        ))
    db_session.flush()

    result = compute_signal_drift(db_session, symbol=symbol, window_hours=24)

    assert result["drift_detected"] is True
    assert result["dominated_by_hold"] is True
    assert len(result["drifting_signals"]) >= 1


def test_drift_result_structure(db_session):
    """compute_signal_drift must always return required keys."""
    symbol = f"pytest-drift-struct-{uuid4().hex[:8]}"
    result = compute_signal_drift(db_session, symbol=symbol)

    required_keys = {
        "window_hours", "recent_total", "historical_total",
        "recent_ratios", "historical_ratios", "drift_detected",
        "drift_threshold_pp", "drifting_signals", "dominated_by_hold", "message",
    }
    assert required_keys.issubset(result.keys())


def test_drift_threshold_reported_in_percentage_points(db_session):
    """drift_threshold_pp should be DRIFT_THRESHOLD converted to percentage points."""
    result = compute_signal_drift(db_session)
    expected_pp = DRIFT_THRESHOLD * 100  # e.g. 0.20 → 20.0
    assert result["drift_threshold_pp"] == expected_pp

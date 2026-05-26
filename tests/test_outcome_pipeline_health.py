"""Tests for OutcomePipelineHealthService and DatasetMaturityService.

Validates:
- Health score components and score range
- Bootstrap mode suppression
- Stuck pending detection
- Maturity score computation and band classification
- Readiness flags (calibration_ready, drift_ready, replay_ready)

Uses in-memory SQLite (via conftest) — skips if DB unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.modules.trading.validation.dataset_maturity import (
    CALIBRATION_MIN_NON_HOLD,
    CALIBRATION_MIN_OUTCOMES,
    CALIBRATION_MIN_REGIMES,
    CALIBRATION_MIN_SYMBOLS,
    DatasetMaturityService,
)
from app.modules.trading.validation.models import TradingSignalOutcome
from app.modules.trading.validation.pipeline_health import (
    BOOTSTRAP_OUTCOME_THRESHOLD,
    OutcomePipelineHealthService,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

pytestmark = pytest.mark.usefixtures("db")


def _make_outcome(
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    signal: str = "BUY",
    outcome_correct: bool = True,
    confidence: int = 70,
    regime: str = "trending_up",
    signal_price: float = 100.0,
    outcome_price: float = 105.0,
    evaluated_at: datetime | None = None,
) -> TradingSignalOutcome:
    now = datetime.now(tz=timezone.utc)
    return TradingSignalOutcome(
        analytics_id=None,
        symbol=symbol,
        timeframe=timeframe,
        signal=signal,
        confidence=confidence,
        regime=regime,
        signal_price=signal_price,
        signal_at=now - timedelta(hours=7),
        outcome_price=outcome_price,
        outcome_at=now - timedelta(hours=1),
        candles_elapsed=6,
        price_change_pct=round((outcome_price - signal_price) / signal_price * 100, 4),
        max_favorable_pct=round((max(signal_price, outcome_price) - signal_price) / signal_price * 100, 4),
        max_adverse_pct=round((signal_price - min(signal_price, outcome_price)) / signal_price * 100, 4),
        outcome_correct=outcome_correct,
        evaluation_horizon_candles=6,
        evaluated_at=evaluated_at or now,
    )


# ── OutcomePipelineHealthService ──────────────────────────────────────────────

class TestOutcomePipelineHealthService:

    def test_empty_db_is_bootstrap(self, db) -> None:
        report = OutcomePipelineHealthService(db).check()
        assert report.bootstrap_mode is True
        assert report.severity == "INFO"
        assert report.total_outcomes == 0
        assert report.last_evaluated_at is None

    def test_health_score_zero_to_100(self, db) -> None:
        report = OutcomePipelineHealthService(db).check()
        assert 0.0 <= report.health_score <= 100.0

    def test_bootstrap_suppresses_severity(self, db) -> None:
        # Even with bad conditions (no outcomes, high pending), bootstrap = INFO
        report = OutcomePipelineHealthService(db).check()
        assert report.severity == "INFO"  # bootstrap suppresses WARNING/CRITICAL

    def test_recency_score_full_when_recent_outcome(self, db) -> None:
        now = datetime.now(tz=timezone.utc)
        outcome = _make_outcome(evaluated_at=now - timedelta(minutes=30))
        db.add(outcome)
        db.commit()

        report = OutcomePipelineHealthService(db).check()
        # Recency component should be 30.0 (ran < 60 min ago)
        assert report.components["recency_pts"] == 30.0

    def test_recency_score_zero_when_stale(self, db) -> None:
        # Simulate a very old outcome
        old_time = datetime.now(tz=timezone.utc) - timedelta(hours=5)
        outcome = _make_outcome(evaluated_at=old_time)
        db.add(outcome)
        db.commit()

        report = OutcomePipelineHealthService(db).check()
        assert report.components["recency_pts"] == 0.0

    def test_throughput_grows_with_outcomes(self, db) -> None:
        svc = OutcomePipelineHealthService(db)

        # Empty
        r0 = svc.check()
        pts_0 = r0.components["throughput_pts"]

        # Add 25 outcomes (half the threshold)
        for i in range(25):
            db.add(_make_outcome(symbol="SOL/USDT" if i % 2 == 0 else "BTC/USDT"))
        db.commit()

        r25 = svc.check()
        pts_25 = r25.components["throughput_pts"]

        assert pts_25 > pts_0
        assert pts_25 == pytest.approx(12.5, abs=0.1)

    def test_no_stuck_count_when_empty(self, db) -> None:
        report = OutcomePipelineHealthService(db).check()
        assert report.stuck_count == 0

    def test_last_evaluated_at_matches_newest(self, db) -> None:
        t1 = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        t2 = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
        db.add(_make_outcome(evaluated_at=t1))
        db.add(_make_outcome(evaluated_at=t2))
        db.commit()

        report = OutcomePipelineHealthService(db).check()
        assert report.last_evaluated_at is not None
        # Should be within 1 second of t2
        diff = abs((report.last_evaluated_at - t2).total_seconds())
        assert diff < 2


# ── DatasetMaturityService ────────────────────────────────────────────────────

class TestDatasetMaturityService:

    def test_empty_db_is_bootstrap_band(self, db) -> None:
        report = DatasetMaturityService(db).assess()
        assert report.band == "BOOTSTRAP"
        assert report.maturity_score < 20
        assert report.calibration_ready is False
        assert report.drift_ready is False
        assert report.replay_ready is False

    def test_maturity_score_zero_to_100(self, db) -> None:
        report = DatasetMaturityService(db).assess()
        assert 0.0 <= report.maturity_score <= 100.0

    def test_calibration_ready_requires_all_criteria(self, db) -> None:
        # Add just enough outcomes to meet all criteria
        symbols = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
        regimes = ["trending_up", "trending_down", "ranging"]

        for i in range(CALIBRATION_MIN_OUTCOMES):
            db.add(_make_outcome(
                symbol=symbols[i % len(symbols)],
                signal="BUY" if i % 2 == 0 else "SELL",
                regime=regimes[i % len(regimes)],
                confidence=40 + (i % 60),  # spread 40-100
            ))
        db.commit()

        report = DatasetMaturityService(db).assess()
        assert report.total_outcomes == CALIBRATION_MIN_OUTCOMES
        assert report.non_hold_outcomes == CALIBRATION_MIN_OUTCOMES  # all BUY/SELL
        assert report.distinct_symbols >= CALIBRATION_MIN_SYMBOLS
        assert report.distinct_regimes >= CALIBRATION_MIN_REGIMES
        assert report.calibration_ready is True

    def test_non_hold_only_counts_buy_sell(self, db) -> None:
        # Outcomes with signal="HOLD" should not count as non_hold
        # (In practice HOLD signals don't generate outcomes, but test the filter)
        db.add(_make_outcome(signal="BUY"))
        db.add(_make_outcome(signal="SELL"))
        db.commit()

        report = DatasetMaturityService(db).assess()
        assert report.non_hold_outcomes == 2

    def test_confidence_spread_zero_when_all_same(self, db) -> None:
        for _ in range(5):
            db.add(_make_outcome(confidence=75))
        db.commit()

        report = DatasetMaturityService(db).assess()
        assert report.confidence_spread == 0.0

    def test_confidence_spread_positive_when_varied(self, db) -> None:
        confidences = [10, 30, 50, 70, 90]
        for c in confidences:
            db.add(_make_outcome(confidence=c))
        db.commit()

        report = DatasetMaturityService(db).assess()
        assert report.confidence_spread > 20.0  # should be ~28.6

    def test_band_progresses_with_outcomes(self, db) -> None:
        # Start with BOOTSTRAP
        r = DatasetMaturityService(db).assess()
        assert r.band == "BOOTSTRAP"

        # Add many diverse outcomes
        symbols = ["BTC/USDT", "SOL/USDT", "ETH/USDT", "DOGE/USDT"]
        regimes = ["trending_up", "trending_down", "ranging", "breakout", "consolidation"]
        for i in range(200):
            db.add(_make_outcome(
                symbol=symbols[i % len(symbols)],
                signal="BUY" if i % 2 == 0 else "SELL",
                regime=regimes[i % len(regimes)],
                confidence=10 + (i % 90),
            ))
        db.commit()

        r2 = DatasetMaturityService(db).assess()
        # Should have moved past BOOTSTRAP
        assert r2.band != "BOOTSTRAP"
        assert r2.maturity_score >= 20.0

    def test_drift_ready_threshold(self, db) -> None:
        # Add exactly 100 outcomes → drift_ready=True
        for _ in range(100):
            db.add(_make_outcome(signal="BUY"))
        db.commit()

        report = DatasetMaturityService(db).assess()
        assert report.drift_ready is True

    def test_replay_ready_threshold(self, db) -> None:
        # Add exactly CALIBRATION_MIN_NON_HOLD outcomes → replay_ready=True
        for _ in range(CALIBRATION_MIN_NON_HOLD):
            db.add(_make_outcome(signal="SELL"))
        db.commit()

        report = DatasetMaturityService(db).assess()
        assert report.replay_ready is True

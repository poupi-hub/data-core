"""Tests for the crypto edge registry — Phase 6 + Phase 7.

Covers:
  - _bucket_label / confidence buckets
  - _sharpe, _max_drawdown
  - _compute_group_metrics
  - _confidence_calibration
  - _assess_edge (GO/NO-GO)
  - compute_edge_outcome with mock DB
  - build_edge_report structure (Phase 6)
  - _regime_intelligence
  - _confidence_intelligence
  - _shadow_strategy_metrics
  - build_phase7_report structure
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.modules.crypto.edge.calculator import (
    CONFIDENCE_BUCKETS,
    HORIZONS_HOURS,
    SHADOW_MIN_CONFIDENCE,
    SHADOW_REGIME,
    _assess_edge,
    _bucket_label,
    _compute_group_metrics,
    _confidence_calibration,
    _confidence_intelligence,
    _max_drawdown,
    _regime_intelligence,
    _shadow_strategy_metrics,
    _sharpe,
    build_edge_report,
    build_phase7_report,
    compute_edge_outcome,
)
from app.modules.crypto.edge.models import SignalEdgeOutcome

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outcome(
    horizon_hours: int = 24,
    price_change_pct: float | None = 5.0,
    mfe_pct: float | None = 8.0,
    mae_pct: float | None = -2.0,
    outcome_correct: bool | None = True,
    confidence: int | None = 82,
    regime: str | None = "TRENDING_UP",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    analytics_id: uuid.UUID | None = None,
) -> SignalEdgeOutcome:
    o = SignalEdgeOutcome()
    o.id = uuid.uuid4()
    o.analytics_id = analytics_id or uuid.uuid4()
    o.horizon_hours = horizon_hours
    o.symbol = symbol
    o.timeframe = timeframe
    o.signal = "BUY"
    o.confidence = confidence
    o.regime = regime
    o.signal_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    o.signal_price = Decimal("60000.0")
    o.price_change_pct = Decimal(str(price_change_pct)) if price_change_pct is not None else None
    o.mfe_pct = Decimal(str(mfe_pct)) if mfe_pct is not None else None
    o.mae_pct = Decimal(str(mae_pct)) if mae_pct is not None else None
    o.outcome_correct = outcome_correct
    o.computed_at = datetime(2026, 3, 2, tzinfo=timezone.utc)
    return o


def _make_analytics(
    regime: str = "TRENDING_UP",
    confidence: int = 82,
    analytics_id: uuid.UUID | None = None,
) -> MagicMock:
    a = MagicMock()
    a.id = analytics_id or uuid.uuid4()
    a.regime = regime
    a.confidence = confidence
    a.signal = "BUY"
    a.symbol = "BTC/USDT"
    a.timeframe = "1h"
    a.calculated_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    a.market_candle_id = None
    a.rsi = None
    a.adx = None
    a.volume_ratio = None
    a.breakout_score = None
    return a


# ---------------------------------------------------------------------------
# TestBucketLabel
# ---------------------------------------------------------------------------


class TestBucketLabel:
    def test_below_threshold_returns_none(self) -> None:
        assert _bucket_label(54) is None

    def test_zero_returns_none(self) -> None:
        assert _bucket_label(0) is None

    def test_bucket_55_64(self) -> None:
        assert _bucket_label(55) == "55-64"
        assert _bucket_label(64) == "55-64"

    def test_bucket_65_74(self) -> None:
        assert _bucket_label(65) == "65-74"
        assert _bucket_label(74) == "65-74"

    def test_bucket_75_84(self) -> None:
        assert _bucket_label(75) == "75-84"
        assert _bucket_label(84) == "75-84"

    def test_bucket_85_plus(self) -> None:
        assert _bucket_label(85) == "85+"
        assert _bucket_label(100) == "85+"

    def test_none_confidence(self) -> None:
        assert _bucket_label(None) is None


# ---------------------------------------------------------------------------
# TestSharpe / TestMaxDrawdown
# ---------------------------------------------------------------------------


class TestSharpe:
    def test_positive_returns(self) -> None:
        result = _sharpe([2.0, 3.0, 4.0, 5.0])
        assert result is not None
        assert result > 0

    def test_single_element_returns_none(self) -> None:
        assert _sharpe([5.0]) is None

    def test_empty_returns_none(self) -> None:
        assert _sharpe([]) is None

    def test_zero_variance_returns_none(self) -> None:
        assert _sharpe([3.0, 3.0, 3.0]) is None


class TestMaxDrawdown:
    def test_no_drawdown(self) -> None:
        assert _max_drawdown([1.0, 2.0, 3.0]) == 0.0

    def test_single_drawdown(self) -> None:
        dd = _max_drawdown([5.0, -10.0, 3.0])
        assert dd > 0

    def test_empty(self) -> None:
        assert _max_drawdown([]) == 0.0


# ---------------------------------------------------------------------------
# TestGroupMetrics
# ---------------------------------------------------------------------------


class TestGroupMetrics:
    def test_all_wins(self) -> None:
        outcomes = [_make_outcome(price_change_pct=5.0, outcome_correct=True) for _ in range(5)]
        m = _compute_group_metrics(outcomes)
        assert m["win_rate"] == 1.0
        assert m["n_evaluated"] == 5

    def test_all_losses(self) -> None:
        outcomes = [
            _make_outcome(price_change_pct=-3.0, outcome_correct=False) for _ in range(4)
        ]
        m = _compute_group_metrics(outcomes)
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] is None

    def test_mixed(self) -> None:
        outcomes = [
            _make_outcome(price_change_pct=10.0, outcome_correct=True),
            _make_outcome(price_change_pct=-5.0, outcome_correct=False),
            _make_outcome(price_change_pct=8.0, outcome_correct=True),
        ]
        m = _compute_group_metrics(outcomes)
        assert m["win_rate"] == pytest.approx(2 / 3, abs=0.01)
        assert m["profit_factor"] is not None
        assert m["profit_factor"] > 1

    def test_empty(self) -> None:
        m = _compute_group_metrics([])
        assert m["n_evaluated"] == 0
        assert m["win_rate"] is None


# ---------------------------------------------------------------------------
# TestConfidenceCalibration
# ---------------------------------------------------------------------------


class TestConfidenceCalibration:
    def test_buckets_present(self) -> None:
        outcomes = [
            _make_outcome(confidence=60, price_change_pct=5.0, outcome_correct=True),
            _make_outcome(confidence=70, price_change_pct=-2.0, outcome_correct=False),
            _make_outcome(confidence=82, price_change_pct=8.0, outcome_correct=True),
            _make_outcome(confidence=90, price_change_pct=12.0, outcome_correct=True),
        ]
        cal = _confidence_calibration(outcomes)
        assert "55-64" in cal
        assert "65-74" in cal
        assert "75-84" in cal
        assert "85+" in cal

    def test_below_threshold_excluded(self) -> None:
        outcomes = [_make_outcome(confidence=30, outcome_correct=True)]
        cal = _confidence_calibration(outcomes)
        for v in cal.values():
            assert v.get("n", 0) == 0

    def test_win_rate_correct(self) -> None:
        outcomes = [
            _make_outcome(confidence=80, price_change_pct=5.0, outcome_correct=True),
            _make_outcome(confidence=78, price_change_pct=-3.0, outcome_correct=False),
        ]
        cal = _confidence_calibration(outcomes)
        assert cal["75-84"]["win_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# TestAssessEdge
# ---------------------------------------------------------------------------


class TestAssessEdge:
    def test_insufficient_data(self) -> None:
        result = _assess_edge([], n_evaluated_24h=0)
        assert result["verdict"] == "INSUFFICIENT_DATA"

    def test_insufficient_data_4(self) -> None:
        result = _assess_edge([], n_evaluated_24h=4)
        assert result["verdict"] == "INSUFFICIENT_DATA"

    def test_edge_detected(self) -> None:
        outcomes = [
            _make_outcome(horizon_hours=24, price_change_pct=5.0, outcome_correct=True)
            for _ in range(7)
        ]
        result = _assess_edge(outcomes, n_evaluated_24h=7)
        assert result["verdict"] == "EDGE_DETECTED"
        assert result["win_rate_24h"] == 1.0

    def test_no_edge(self) -> None:
        outcomes = [
            _make_outcome(horizon_hours=24, price_change_pct=-5.0, outcome_correct=False)
            for _ in range(6)
        ]
        result = _assess_edge(outcomes, n_evaluated_24h=6)
        assert result["verdict"] == "NO_EDGE"

    def test_inconclusive(self) -> None:
        outcomes = [
            _make_outcome(horizon_hours=24, price_change_pct=1.0, outcome_correct=True),
            _make_outcome(horizon_hours=24, price_change_pct=-1.0, outcome_correct=False),
            _make_outcome(horizon_hours=24, price_change_pct=0.5, outcome_correct=True),
            _make_outcome(horizon_hours=24, price_change_pct=-0.3, outcome_correct=False),
            _make_outcome(horizon_hours=24, price_change_pct=0.2, outcome_correct=True),
        ]
        result = _assess_edge(outcomes, n_evaluated_24h=5)
        assert result["verdict"] in ("EDGE_DETECTED", "INCONCLUSIVE")


# ---------------------------------------------------------------------------
# TestComputeEdgeOutcome
# ---------------------------------------------------------------------------


class FakeCandle:
    def __init__(self, ts: datetime, close: float, high: float, low: float) -> None:
        self.id = uuid.uuid4()
        self.timestamp = ts
        self.close = Decimal(str(close))
        self.high = Decimal(str(high))
        self.low = Decimal(str(low))
        self.symbol = "BTC/USDT"
        self.timeframe = "1h"


class FakeAnalytics:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.market_candle_id = uuid.uuid4()
        self.symbol = "BTC/USDT"
        self.timeframe = "1h"
        self.signal = "BUY"
        self.confidence = 82
        self.regime = "TRENDING_UP"
        self.calculated_at = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class TestComputeEdgeOutcome:
    def _make_mock_db(
        self,
        signal_candle_close: float,
        future_candles: list[tuple[float, float, float]],
    ) -> MagicMock:
        mock_db = MagicMock()
        signal_candle = FakeCandle(
            datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
            close=signal_candle_close,
            high=signal_candle_close * 1.01,
            low=signal_candle_close * 0.99,
        )
        future = [
            FakeCandle(
                datetime(2026, 3, 1, 12 + i + 1, 0, tzinfo=timezone.utc),
                close=c[0],
                high=c[1],
                low=c[2],
            )
            for i, c in enumerate(future_candles)
        ]
        mock_db.get.return_value = signal_candle
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = future
        mock_db.query.return_value = mock_query
        return mock_db

    def test_outcome_correct_when_price_rises(self) -> None:
        analytics = FakeAnalytics()
        db = self._make_mock_db(
            signal_candle_close=60000.0,
            future_candles=[
                (61000.0, 62000.0, 59500.0),
                (62000.0, 63000.0, 60000.0),
                (61500.0, 62500.0, 60500.0),
            ],
        )
        now = datetime(2026, 3, 5, tzinfo=timezone.utc)
        outcome = compute_edge_outcome(db, analytics, horizon_hours=24, now=now)
        assert outcome is not None
        assert outcome.outcome_correct is True
        assert float(outcome.price_change_pct) > 0
        assert float(outcome.mfe_pct) > 0
        assert float(outcome.mae_pct) < 0

    def test_outcome_incorrect_when_price_falls(self) -> None:
        analytics = FakeAnalytics()
        db = self._make_mock_db(
            signal_candle_close=60000.0,
            future_candles=[
                (58000.0, 59000.0, 57000.0),
                (57000.0, 58000.0, 56000.0),
                (56000.0, 57000.0, 55000.0),
            ],
        )
        now = datetime(2026, 3, 5, tzinfo=timezone.utc)
        outcome = compute_edge_outcome(db, analytics, horizon_hours=24, now=now)
        assert outcome is not None
        assert outcome.outcome_correct is False
        assert float(outcome.price_change_pct) < 0

    def test_horizon_not_closed_returns_none(self) -> None:
        analytics = FakeAnalytics()
        db = MagicMock()
        now = datetime(2026, 3, 1, 13, 0, tzinfo=timezone.utc)
        outcome = compute_edge_outcome(db, analytics, horizon_hours=24, now=now)
        assert outcome is None

    def test_no_candles_returns_none(self) -> None:
        analytics = FakeAnalytics()
        db = MagicMock()
        signal_candle = FakeCandle(
            datetime(2026, 3, 1, 12, tzinfo=timezone.utc), 60000.0, 61000.0, 59000.0
        )
        db.get.return_value = signal_candle
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []
        db.query.return_value = mock_query
        now = datetime(2026, 3, 5, tzinfo=timezone.utc)
        outcome = compute_edge_outcome(db, analytics, horizon_hours=24, now=now)
        assert outcome is None


# ---------------------------------------------------------------------------
# TestBuildEdgeReport (Phase 6 backward compat)
# ---------------------------------------------------------------------------


class TestBuildEdgeReport:
    def _empty_db(self) -> MagicMock:
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []
        mock_db.query.return_value = mock_query
        return mock_db

    def test_empty_report_structure(self) -> None:
        report = build_edge_report(self._empty_db())
        assert "generated_at" in report
        assert "summary" in report
        assert "edge_registry" in report
        assert "confidence_calibration" in report
        assert "quant_metrics" in report
        assert "go_no_go" in report
        assert report["summary"]["total_buy_signals"] == 0
        assert report["go_no_go"]["verdict"] == "INSUFFICIENT_DATA"

    def test_calibration_keys_present(self) -> None:
        report = build_edge_report(self._empty_db())
        cal = report["confidence_calibration"]
        for h in HORIZONS_HOURS:
            key = f"{h}h"
            assert key in cal, f"calibration missing horizon key '{key}'"
            for bucket_label, _, _ in CONFIDENCE_BUCKETS:
                assert bucket_label in cal[key], f"calibration missing bucket '{bucket_label}'"

    def test_quant_metrics_breakdowns_present(self) -> None:
        report = build_edge_report(self._empty_db())
        qm = report["quant_metrics"]
        for h in HORIZONS_HOURS:
            assert f"overall_{h}h" in qm
        assert "by_symbol" in qm
        assert "by_timeframe" in qm
        assert "by_regime" in qm
        assert "by_confidence_bucket" in qm


# ---------------------------------------------------------------------------
# TestRegimeIntelligence (Phase 7)
# ---------------------------------------------------------------------------


class TestRegimeIntelligence:
    def test_groups_by_regime(self) -> None:
        outcomes = [
            _make_outcome(regime="TRENDING_UP", horizon_hours=24, outcome_correct=True),
            _make_outcome(regime="TRENDING_UP", horizon_hours=24, outcome_correct=False),
            _make_outcome(regime="UNKNOWN", horizon_hours=24, outcome_correct=True),
        ]
        result = _regime_intelligence(outcomes, horizons=[24])
        assert "TRENDING_UP" in result
        assert "UNKNOWN" in result
        assert result["TRENDING_UP"]["by_horizon"]["24h"]["n_signals"] == 2
        assert result["UNKNOWN"]["by_horizon"]["24h"]["n_signals"] == 1

    def test_all_horizons_present(self) -> None:
        outcomes = [
            _make_outcome(regime="TRENDING_UP", horizon_hours=h)
            for h in [24, 72, 168, 336]
        ]
        result = _regime_intelligence(outcomes, horizons=[24, 72, 168, 336])
        assert "TRENDING_UP" in result
        for h in [24, 72, 168, 336]:
            assert f"{h}h" in result["TRENDING_UP"]["by_horizon"]

    def test_empty_outcomes(self) -> None:
        result = _regime_intelligence([], horizons=[24, 72])
        assert result == {}

    def test_win_rate_correct(self) -> None:
        outcomes = [
            _make_outcome(
                regime="UNKNOWN", horizon_hours=24,
                price_change_pct=5.0, outcome_correct=True,
            ),
            _make_outcome(
                regime="UNKNOWN", horizon_hours=24,
                price_change_pct=-3.0, outcome_correct=False,
            ),
            _make_outcome(
                regime="UNKNOWN", horizon_hours=24,
                price_change_pct=2.0, outcome_correct=True,
            ),
        ]
        result = _regime_intelligence(outcomes, horizons=[24])
        m = result["UNKNOWN"]["by_horizon"]["24h"]
        assert m["win_rate"] == pytest.approx(2 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# TestConfidenceIntelligence (Phase 7)
# ---------------------------------------------------------------------------


class TestConfidenceIntelligence:
    def test_all_buckets_present(self) -> None:
        outcomes = [
            _make_outcome(confidence=60, horizon_hours=24),
            _make_outcome(confidence=70, horizon_hours=24),
            _make_outcome(confidence=80, horizon_hours=24),
            _make_outcome(confidence=90, horizon_hours=24),
        ]
        result = _confidence_intelligence(outcomes, horizons=[24])
        for label, _, _ in CONFIDENCE_BUCKETS:
            assert label in result
            assert "by_horizon" in result[label]
            assert "24h" in result[label]["by_horizon"]

    def test_total_signals_correct(self) -> None:
        aid = uuid.uuid4()
        outcomes = [
            _make_outcome(confidence=80, horizon_hours=24, analytics_id=aid),
            _make_outcome(confidence=80, horizon_hours=72, analytics_id=aid),
        ]
        result = _confidence_intelligence(outcomes, horizons=[24, 72])
        # Same analytics_id → 1 unique signal
        assert result["75-84"]["total_signals"] == 1

    def test_empty_bucket_has_zero(self) -> None:
        outcomes = [_make_outcome(confidence=80, horizon_hours=24)]
        result = _confidence_intelligence(outcomes, horizons=[24])
        assert result["55-64"]["total_signals"] == 0
        assert result["65-74"]["total_signals"] == 0
        assert result["85+"]["total_signals"] == 0


# ---------------------------------------------------------------------------
# TestShadowStrategy (Phase 7)
# ---------------------------------------------------------------------------


class TestShadowStrategy:
    def test_filter_constants(self) -> None:
        assert SHADOW_REGIME == "UNKNOWN"
        assert SHADOW_MIN_CONFIDENCE == 75

    def test_shadow_separates_correctly(self) -> None:
        shadow_id = uuid.uuid4()
        non_shadow_id = uuid.uuid4()
        shadow_analytics = _make_analytics(
            regime="UNKNOWN", confidence=80, analytics_id=shadow_id
        )
        non_shadow_analytics = _make_analytics(
            regime="TRENDING_UP", confidence=90, analytics_id=non_shadow_id
        )
        outcomes = [
            _make_outcome(
                regime="UNKNOWN", confidence=80, horizon_hours=72,
                price_change_pct=8.0, outcome_correct=True, analytics_id=shadow_id,
            ),
            _make_outcome(
                regime="TRENDING_UP", confidence=90, horizon_hours=72,
                price_change_pct=-3.0, outcome_correct=False, analytics_id=non_shadow_id,
            ),
        ]
        result = _shadow_strategy_metrics(
            outcomes, [shadow_analytics, non_shadow_analytics], horizons=[72]
        )
        assert result["n_shadow_signals"] == 1
        assert result["n_excluded_signals"] == 1
        shadow_m = result["shadow_metrics_by_horizon"]["72h"]
        assert shadow_m["win_rate"] == 1.0
        current_m = result["current_strategy_excl_shadow_by_horizon"]["72h"]
        assert current_m["win_rate"] == 0.0

    def test_below_confidence_threshold_excluded_from_shadow(self) -> None:
        aid = uuid.uuid4()
        analytics = _make_analytics(regime="UNKNOWN", confidence=70, analytics_id=aid)
        outcomes = [
            _make_outcome(regime="UNKNOWN", confidence=70, horizon_hours=24, analytics_id=aid)
        ]
        result = _shadow_strategy_metrics(outcomes, [analytics], horizons=[24])
        assert result["n_shadow_signals"] == 0

    def test_note_is_present(self) -> None:
        result = _shadow_strategy_metrics([], [], horizons=[24])
        assert "OBSERVATION ONLY" in result["note"]

    def test_filter_description_correct(self) -> None:
        result = _shadow_strategy_metrics([], [], horizons=[24])
        assert "UNKNOWN" in result["filter"]
        assert "75" in result["filter"]


# ---------------------------------------------------------------------------
# TestBuildPhase7Report (Phase 7 integration)
# ---------------------------------------------------------------------------


class TestBuildPhase7Report:
    def _empty_db(self) -> MagicMock:
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []
        mock_db.query.return_value = mock_query
        return mock_db

    def test_report_top_level_keys(self) -> None:
        report = build_phase7_report(self._empty_db())
        required_keys = [
            "generated_at", "summary", "regime_intelligence",
            "confidence_intelligence", "shadow_strategy",
            "strategy_comparison", "go_no_go_by_segment",
            "best_regime", "best_bucket", "overall_verdict",
            "overall_metrics_72h", "outcomes",
        ]
        for key in required_keys:
            assert key in report, f"missing key: {key}"

    def test_empty_report_verdict_insufficient(self) -> None:
        report = build_phase7_report(self._empty_db())
        assert report["overall_verdict"] == "INSUFFICIENT_DATA"

    def test_summary_has_shadow_info(self) -> None:
        report = build_phase7_report(self._empty_db())
        assert "shadow_filter" in report["summary"]
        assert "UNKNOWN" in report["summary"]["shadow_filter"]
        assert "n_shadow_signals" in report["summary"]

    def test_strategy_comparison_has_horizons(self) -> None:
        report = build_phase7_report(self._empty_db())
        # With empty data, strategy_comparison is empty (no outcomes)
        # but the key must exist
        assert "strategy_comparison" in report

    def test_go_no_go_by_segment_structure(self) -> None:
        report = build_phase7_report(self._empty_db())
        gng = report["go_no_go_by_segment"]
        assert isinstance(gng, dict)
        # Should have shadow keys for each horizon
        for h in [24, 72, 168, 336]:
            assert f"shadow_{h}h" in gng

    def test_336h_in_horizons(self) -> None:
        assert 336 in HORIZONS_HOURS

"""Tests for Phase 8 Forward Shadow Validation."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.modules.crypto.edge.forward import (
    ALERT_PF_MIN,
    ALERT_WR_MIN,
    FORWARD_CONF_MAX,
    FORWARD_CONF_MIN,
    FORWARD_HORIZONS,
    FORWARD_REGIME,
    ForwardShadowTracker,
    _edge_stability_verdict,
    _horizon_metrics,
    build_forward_validation_report,
)
from app.modules.crypto.edge.forward_model import ForwardShadowSignal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_shadow(
    confidence: int = 80,
    regime: str = "UNKNOWN",
    return_24h: float | None = None,
    outcome_correct_24h: bool | None = None,
    return_72h: float | None = None,
    outcome_correct_72h: bool | None = None,
    return_168h: float | None = None,
    outcome_correct_168h: bool | None = None,
    alert_entry_sent: bool = False,
    alert_24h_sent: bool = False,
    alert_72h_sent: bool = False,
    alert_168h_sent: bool = False,
) -> ForwardShadowSignal:
    s = ForwardShadowSignal()
    s.id = uuid.uuid4()
    s.analytics_id = uuid.uuid4()
    s.symbol = "BTC/USDT"
    s.timeframe = "1h"
    s.confidence = confidence
    s.regime = regime
    s.signal_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    s.signal_price = Decimal("60000")
    s.return_24h = Decimal(str(return_24h)) if return_24h is not None else None
    s.outcome_correct_24h = outcome_correct_24h
    s.return_72h = Decimal(str(return_72h)) if return_72h is not None else None
    s.outcome_correct_72h = outcome_correct_72h
    s.return_168h = Decimal(str(return_168h)) if return_168h is not None else None
    s.outcome_correct_168h = outcome_correct_168h
    s.mfe_24h = None
    s.mae_24h = None
    s.mfe_72h = None
    s.mae_72h = None
    s.mfe_168h = None
    s.mae_168h = None
    s.alert_entry_sent = alert_entry_sent
    s.alert_24h_sent = alert_24h_sent
    s.alert_72h_sent = alert_72h_sent
    s.alert_168h_sent = alert_168h_sent
    s.created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    s.updated_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return s


# ---------------------------------------------------------------------------
# TestConstants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_filter_regime(self) -> None:
        assert FORWARD_REGIME == "UNKNOWN"

    def test_confidence_range(self) -> None:
        assert FORWARD_CONF_MIN == 75
        assert FORWARD_CONF_MAX == 84

    def test_horizons(self) -> None:
        assert FORWARD_HORIZONS == [24, 72, 168]

    def test_alert_thresholds(self) -> None:
        assert ALERT_WR_MIN == 0.55
        assert ALERT_PF_MIN == 1.5


# ---------------------------------------------------------------------------
# TestHorizonMetrics
# ---------------------------------------------------------------------------


class TestHorizonMetrics:
    def test_empty_returns_insufficient(self) -> None:
        m = _horizon_metrics([], 24)
        assert m["n_evaluated"] == 0
        assert m["go_no_go"] == "INSUFFICIENT_DATA"
        assert m["win_rate"] is None

    def test_pending_all(self) -> None:
        rows = [_make_shadow() for _ in range(3)]
        m = _horizon_metrics(rows, 24)
        assert m["n_pending"] == 3
        assert m["n_evaluated"] == 0

    def test_win_rate_all_correct(self) -> None:
        rows = [
            _make_shadow(return_24h=5.0, outcome_correct_24h=True) for _ in range(6)
        ]
        m = _horizon_metrics(rows, 24)
        assert m["win_rate"] == 1.0
        assert m["n_evaluated"] == 6
        assert m["go_no_go"] == "GO"

    def test_win_rate_all_wrong(self) -> None:
        rows = [
            _make_shadow(return_24h=-3.0, outcome_correct_24h=False) for _ in range(6)
        ]
        m = _horizon_metrics(rows, 24)
        assert m["win_rate"] == 0.0
        assert "WR_BELOW_55" in m["alert_flags"]
        assert "AVG_RETURN_NEGATIVE" in m["alert_flags"]
        assert m["go_no_go"] == "NO_GO"

    def test_pf_alert_when_below_threshold(self) -> None:
        rows = [
            _make_shadow(return_24h=1.0, outcome_correct_24h=True),
            _make_shadow(return_24h=1.0, outcome_correct_24h=True),
            _make_shadow(return_24h=-3.0, outcome_correct_24h=False),
            _make_shadow(return_24h=-3.0, outcome_correct_24h=False),
            _make_shadow(return_24h=-3.0, outcome_correct_24h=False),
        ]
        m = _horizon_metrics(rows, 24)
        # pf = 2/9 < 1.5
        assert "PF_BELOW_1.5" in m["alert_flags"]

    def test_insufficient_data_below_5(self) -> None:
        rows = [
            _make_shadow(return_24h=5.0, outcome_correct_24h=True) for _ in range(4)
        ]
        m = _horizon_metrics(rows, 24)
        assert m["go_no_go"] == "INSUFFICIENT_DATA"

    def test_72h_metrics(self) -> None:
        rows = [
            _make_shadow(return_72h=8.0, outcome_correct_72h=True) for _ in range(5)
        ]
        m = _horizon_metrics(rows, 72)
        assert m["win_rate"] == 1.0
        assert m["go_no_go"] == "GO"
        assert m["profit_factor"] is None  # all wins, no losses

    def test_168h_pending_does_not_affect_24h(self) -> None:
        rows = [
            _make_shadow(
                return_24h=5.0,
                outcome_correct_24h=True,
                # 168h still pending
            )
            for _ in range(5)
        ]
        m24 = _horizon_metrics(rows, 24)
        m168 = _horizon_metrics(rows, 168)
        assert m24["n_evaluated"] == 5
        assert m168["n_evaluated"] == 0


# ---------------------------------------------------------------------------
# TestEdgeStabilityVerdict
# ---------------------------------------------------------------------------


class TestEdgeStabilityVerdict:
    def test_all_insufficient(self) -> None:
        h = {
            "24h": {"go_no_go": "INSUFFICIENT_DATA"},
            "72h": {"go_no_go": "INSUFFICIENT_DATA"},
            "168h": {"go_no_go": "INSUFFICIENT_DATA"},
        }
        assert _edge_stability_verdict(h) == "INSUFFICIENT_DATA"

    def test_any_no_go(self) -> None:
        h = {"24h": {"go_no_go": "GO"}, "72h": {"go_no_go": "NO_GO"}, "168h": {"go_no_go": "GO"}}
        assert _edge_stability_verdict(h) == "NO_GO"

    def test_all_go(self) -> None:
        h = {"24h": {"go_no_go": "GO"}, "72h": {"go_no_go": "GO"}, "168h": {"go_no_go": "GO"}}
        assert _edge_stability_verdict(h) == "GO"

    def test_mixed_go_insufficient(self) -> None:
        h = {
            "24h": {"go_no_go": "GO"},
            "72h": {"go_no_go": "GO"},
            "168h": {"go_no_go": "INSUFFICIENT_DATA"},
        }
        assert _edge_stability_verdict(h) == "GO"

    def test_inconclusive(self) -> None:
        h = {
            "24h": {"go_no_go": "GO"},
            "72h": {"go_no_go": "INSUFFICIENT_DATA"},
            "168h": {"go_no_go": "NO_GO"},
        }
        assert _edge_stability_verdict(h) == "NO_GO"


# ---------------------------------------------------------------------------
# TestForwardShadowTrackerCapture
# ---------------------------------------------------------------------------


class TestForwardShadowTrackerCapture:
    def _mock_db(self, analytics_rows: list, existing_ids: list | None = None) -> MagicMock:
        db = MagicMock()
        existing = existing_ids or []

        def query_side_effect(model):  # noqa: ANN001
            q = MagicMock()
            q.all.return_value = [(uuid.UUID(i),) for i in existing] if existing else []
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q

            from app.modules.crypto.edge.forward_model import ForwardShadowSignal as FSS

            if model is FSS:
                q.all.return_value = []  # no existing shadows
            else:
                # TradingAnalytics
                q.all.return_value = analytics_rows

            return q

        db.query.side_effect = query_side_effect
        db.get.return_value = None
        db.add = MagicMock()
        db.commit = MagicMock()
        return db

    def test_new_signal_captured(self) -> None:
        from app.analytics.models import TradingAnalytics

        a = MagicMock(spec=TradingAnalytics)
        a.id = uuid.uuid4()
        a.symbol = "BTC/USDT"
        a.timeframe = "1h"
        a.confidence = 80
        a.regime = "UNKNOWN"
        a.signal = "BUY"
        a.calculated_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
        a.market_candle_id = None

        db = self._mock_db([a])
        tracker = ForwardShadowTracker()
        count = tracker._capture_new_signals(db)
        assert count == 1
        db.add.assert_called_once()

    def test_already_existing_not_recaptured(self) -> None:
        """If analytics_id already in forward_shadow_signals, skip it."""
        from app.analytics.models import TradingAnalytics

        aid = uuid.uuid4()
        a = MagicMock(spec=TradingAnalytics)
        a.id = aid
        a.symbol = "BTC/USDT"
        a.timeframe = "1h"
        a.confidence = 80
        a.regime = "UNKNOWN"
        a.signal = "BUY"
        a.calculated_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
        a.market_candle_id = None

        # Simulate analytics already tracked
        db = MagicMock()
        existing_shadow = MagicMock()
        existing_shadow.analytics_id = aid

        def query_side_effect(model):  # noqa: ANN001
            q = MagicMock()
            from app.modules.crypto.edge.forward_model import ForwardShadowSignal as FSS

            if model is FSS:
                q.all.return_value = [existing_shadow]
            else:
                q.all.return_value = [a]
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            return q

        db.query.side_effect = query_side_effect
        db.add = MagicMock()
        db.commit = MagicMock()

        tracker = ForwardShadowTracker()
        count = tracker._capture_new_signals(db)
        assert count == 0
        db.add.assert_not_called()


# ---------------------------------------------------------------------------
# TestBuildForwardValidationReport
# ---------------------------------------------------------------------------


class TestBuildForwardValidationReport:
    def _empty_db(self) -> MagicMock:
        db = MagicMock()
        q = MagicMock()
        q.all.return_value = []
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        db.query.return_value = q
        db.get.return_value = None
        db.add = MagicMock()
        db.commit = MagicMock()
        return db

    def test_report_structure(self) -> None:
        db = self._empty_db()
        report = build_forward_validation_report(db, run_tracker=False)
        assert "generated_at" in report
        assert "filter" in report
        assert "n_signals_tracked" in report
        assert "horizons" in report
        assert "edge_stability" in report
        assert "active_alerts" in report
        assert "signals" in report

    def test_filter_values(self) -> None:
        db = self._empty_db()
        report = build_forward_validation_report(db, run_tracker=False)
        f = report["filter"]
        assert f["regime"] == "UNKNOWN"
        assert f["confidence_min"] == 75
        assert f["confidence_max"] == 84
        assert f["signal"] == "BUY"

    def test_horizon_keys_present(self) -> None:
        db = self._empty_db()
        report = build_forward_validation_report(db, run_tracker=False)
        for h in [24, 72, 168]:
            assert f"{h}h" in report["horizons"]

    def test_empty_db_insufficient_data(self) -> None:
        db = self._empty_db()
        report = build_forward_validation_report(db, run_tracker=False)
        assert report["edge_stability"] == "INSUFFICIENT_DATA"
        assert report["n_signals_tracked"] == 0

    def test_run_tracker_false_skips_tracker(self) -> None:
        db = self._empty_db()
        report = build_forward_validation_report(db, run_tracker=False)
        assert report["tracker_run"] == {}

    def test_signals_list_populated(self) -> None:
        shadow = _make_shadow(return_24h=5.0, outcome_correct_24h=True)
        db = MagicMock()
        q = MagicMock()
        q.all.return_value = [shadow]
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        db.query.return_value = q
        db.get.return_value = None
        db.add = MagicMock()
        db.commit = MagicMock()
        report = build_forward_validation_report(db, run_tracker=False)
        assert report["n_signals_tracked"] == 1
        assert len(report["signals"]) == 1
        s = report["signals"][0]
        assert s["return_24h"] == 5.0
        assert s["outcome_correct_24h"] is True

"""Tests for Phase 9 Forward Validation Readiness Panel."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.modules.crypto.edge.forward_model import ForwardShadowSignal
from app.modules.crypto.edge.readiness import (
    EDGE_DETECTED,
    EDGE_INSUFFICIENT,
    EDGE_NO_EDGE,
    EDGE_POSSIBLE,
    READINESS_BOOTSTRAP,
    READINESS_EARLY,
    READINESS_MODERATE,
    READINESS_RELEVANT,
    _edge_status,
    _gates,
    _horizon_readiness,
    _normal_ci,
    _overall_verdict,
    _pf_ci,
    _readiness_score,
    _wilson_ci,
    build_daily_summary_message,
    build_readiness_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_shadow(
    return_24h: float | None = None,
    outcome_correct_24h: bool | None = None,
    return_72h: float | None = None,
    outcome_correct_72h: bool | None = None,
    return_168h: float | None = None,
    outcome_correct_168h: bool | None = None,
) -> ForwardShadowSignal:
    s = ForwardShadowSignal()
    s.id = uuid.uuid4()
    s.analytics_id = uuid.uuid4()
    s.symbol = "BTC/USDT"
    s.timeframe = "1h"
    s.confidence = 80
    s.regime = "UNKNOWN"
    s.signal_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    s.signal_price = Decimal("60000")
    s.return_24h = Decimal(str(return_24h)) if return_24h is not None else None
    s.outcome_correct_24h = outcome_correct_24h
    s.return_72h = Decimal(str(return_72h)) if return_72h is not None else None
    s.outcome_correct_72h = outcome_correct_72h
    s.return_168h = Decimal(str(return_168h)) if return_168h is not None else None
    s.outcome_correct_168h = outcome_correct_168h
    s.mfe_24h = s.mae_24h = s.mfe_72h = s.mae_72h = s.mfe_168h = s.mae_168h = None
    s.alert_entry_sent = s.alert_24h_sent = s.alert_72h_sent = s.alert_168h_sent = False
    s.created_at = s.updated_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return s


def _make_n_shadows(
    n: int,
    *,
    wr: float = 0.75,
    avg_return: float = 3.0,
    horizon: int = 72,
) -> list[ForwardShadowSignal]:
    """Generate n shadows with the given win rate and avg return for one horizon."""
    rows = []
    n_wins = round(n * wr)
    for i in range(n):
        correct = i < n_wins
        ret = avg_return if correct else -abs(avg_return) * 0.5
        kwargs = {
            f"return_{horizon}h": ret,
            f"outcome_correct_{horizon}h": correct,
        }
        rows.append(_make_shadow(**kwargs))
    return rows


# ---------------------------------------------------------------------------
# TestWilsonCI
# ---------------------------------------------------------------------------


class TestWilsonCI:
    def test_all_successes(self) -> None:
        lo, hi = _wilson_ci(10, 10)
        assert hi <= 1.0
        assert lo > 0.5

    def test_no_successes(self) -> None:
        lo, hi = _wilson_ci(0, 10)
        assert lo == 0.0
        assert hi < 0.5

    def test_zero_total(self) -> None:
        lo, hi = _wilson_ci(0, 0)
        assert lo == 0.0
        assert hi == 1.0

    def test_bounds_valid(self) -> None:
        for n_s in range(0, 11):
            lo, hi = _wilson_ci(n_s, 10)
            assert 0.0 <= lo <= hi <= 1.0

    def test_symmetric_near_half(self) -> None:
        lo, hi = _wilson_ci(5, 10)
        mid = (lo + hi) / 2
        assert abs(mid - 0.5) < 0.05


# ---------------------------------------------------------------------------
# TestNormalCI
# ---------------------------------------------------------------------------


class TestNormalCI:
    def test_single_value_none(self) -> None:
        assert _normal_ci([5.0]) is None

    def test_empty_none(self) -> None:
        assert _normal_ci([]) is None

    def test_symmetric(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        lo, hi = _normal_ci(values)
        mid = (lo + hi) / 2
        assert abs(mid - 3.0) < 0.01

    def test_width_decreases_with_n(self) -> None:
        import random

        random.seed(42)
        small = [random.gauss(0, 1) for _ in range(10)]
        large = small * 10
        lo_s, hi_s = _normal_ci(small)
        lo_l, hi_l = _normal_ci(large)
        assert (hi_s - lo_s) > (hi_l - lo_l)


# ---------------------------------------------------------------------------
# TestPfCI
# ---------------------------------------------------------------------------


class TestPfCI:
    def test_no_wins_returns_none(self) -> None:
        assert _pf_ci([], [-1.0, -2.0]) is None

    def test_no_losses_returns_none(self) -> None:
        assert _pf_ci([1.0, 2.0], []) is None

    def test_bounds_positive(self) -> None:
        lo, hi = _pf_ci([3.0, 4.0, 5.0], [-1.0, -2.0])
        assert lo >= 0.0
        assert hi > lo


# ---------------------------------------------------------------------------
# TestReadinessScore
# ---------------------------------------------------------------------------


class TestReadinessScore:
    def test_bootstrap(self) -> None:
        for n in [0, 1, 5, 9]:
            assert _readiness_score(n) == READINESS_BOOTSTRAP

    def test_early(self) -> None:
        for n in [10, 15, 29]:
            assert _readiness_score(n) == READINESS_EARLY

    def test_moderate(self) -> None:
        for n in [30, 50, 99]:
            assert _readiness_score(n) == READINESS_MODERATE

    def test_relevant(self) -> None:
        for n in [100, 200, 1000]:
            assert _readiness_score(n) == READINESS_RELEVANT


# ---------------------------------------------------------------------------
# TestGates
# ---------------------------------------------------------------------------


class TestGates:
    def test_all_false_below_10(self) -> None:
        g = _gates(9)
        assert g == {"n_ge_10": False, "n_ge_30": False, "n_ge_100": False}

    def test_only_10_at_10(self) -> None:
        g = _gates(10)
        assert g["n_ge_10"] is True
        assert g["n_ge_30"] is False
        assert g["n_ge_100"] is False

    def test_all_true_at_100(self) -> None:
        g = _gates(100)
        assert all(g.values())

    def test_gates_30(self) -> None:
        g = _gates(30)
        assert g["n_ge_10"] is True
        assert g["n_ge_30"] is True
        assert g["n_ge_100"] is False


# ---------------------------------------------------------------------------
# TestEdgeStatus
# ---------------------------------------------------------------------------


class TestEdgeStatus:
    def test_insufficient_below_10(self) -> None:
        assert _edge_status(9, 0.8, 3.0, (0.6, 1.0)) == EDGE_INSUFFICIENT

    def test_insufficient_no_wr(self) -> None:
        assert _edge_status(15, None, None, None) == EDGE_INSUFFICIENT

    def test_no_edge_low_wr(self) -> None:
        assert _edge_status(20, 0.45, 0.9, (0.3, 0.6)) == EDGE_NO_EDGE

    def test_no_edge_pf_below_1(self) -> None:
        assert _edge_status(20, 0.6, 0.8, (0.4, 0.8)) == EDGE_NO_EDGE

    def test_possible_edge_low_n(self) -> None:
        # WR ≥ 50%, but n < 30
        assert _edge_status(15, 0.70, 2.0, (0.5, 0.9)) == EDGE_POSSIBLE

    def test_edge_detected(self) -> None:
        # n ≥ 30, WR ≥ 55%, CI lower > 0.45, PF ≥ 1.5
        assert _edge_status(30, 0.70, 2.5, (0.50, 0.85)) == EDGE_DETECTED

    def test_not_detected_ci_too_low(self) -> None:
        # n ≥ 30 but CI lower < 0.45
        assert _edge_status(30, 0.55, 2.0, (0.30, 0.80)) == EDGE_POSSIBLE


# ---------------------------------------------------------------------------
# TestHorizonReadiness
# ---------------------------------------------------------------------------


class TestHorizonReadiness:
    def test_empty_list(self) -> None:
        h = _horizon_readiness([], 24)
        assert h["n_evaluated"] == 0
        assert h["readiness_score"] == READINESS_BOOTSTRAP
        assert h["edge_status"] == EDGE_INSUFFICIENT

    def test_pending_not_evaluated(self) -> None:
        rows = [_make_shadow() for _ in range(5)]
        h = _horizon_readiness(rows, 24)
        assert h["n_pending"] == 5
        assert h["n_evaluated"] == 0

    def test_all_wins_72h(self) -> None:
        rows = [
            _make_shadow(return_72h=3.0, outcome_correct_72h=True) for _ in range(6)
        ]
        h = _horizon_readiness(rows, 72)
        assert h["win_rate"] == 1.0
        assert h["n_wins"] == 6
        assert h["n_losses"] == 0
        assert h["profit_factor"] is None  # all wins

    def test_ci_present_for_n_ge_2(self) -> None:
        rows = [
            _make_shadow(return_72h=3.0, outcome_correct_72h=True),
            _make_shadow(return_72h=-1.0, outcome_correct_72h=False),
        ]
        h = _horizon_readiness(rows, 72)
        assert h["win_rate_ci_95"] is not None
        lo, hi = h["win_rate_ci_95"]
        assert 0.0 <= lo <= hi <= 1.0

    def test_edge_detected_at_30_wins(self) -> None:
        rows = _make_n_shadows(35, wr=0.70, avg_return=5.0, horizon=72)
        h = _horizon_readiness(rows, 72)
        assert h["edge_status"] == EDGE_DETECTED
        assert h["readiness_score"] == READINESS_MODERATE

    def test_gates_structure(self) -> None:
        rows = _make_n_shadows(10, wr=0.70, horizon=72)
        h = _horizon_readiness(rows, 72)
        assert "gates" in h
        assert h["gates"]["n_ge_10"] is True
        assert h["gates"]["n_ge_30"] is False

    def test_pf_correct(self) -> None:
        # 3 wins of +10, 1 loss of -5 → PF = 30/5 = 6.0
        rows = [
            _make_shadow(return_72h=10.0, outcome_correct_72h=True),
            _make_shadow(return_72h=10.0, outcome_correct_72h=True),
            _make_shadow(return_72h=10.0, outcome_correct_72h=True),
            _make_shadow(return_72h=-5.0, outcome_correct_72h=False),
        ]
        h = _horizon_readiness(rows, 72)
        assert h["profit_factor"] is not None
        assert abs(h["profit_factor"] - 6.0) < 0.01


# ---------------------------------------------------------------------------
# TestOverallVerdict
# ---------------------------------------------------------------------------


class TestOverallVerdict:
    def _h(self, status: str) -> dict:
        return {"edge_status": status}

    def test_all_insufficient(self) -> None:
        blocks = {
            "24h": self._h(EDGE_INSUFFICIENT),
            "72h": self._h(EDGE_INSUFFICIENT),
            "168h": self._h(EDGE_INSUFFICIENT),
        }
        assert _overall_verdict(blocks) == "INSUFFICIENT_DATA"

    def test_no_go_on_no_edge(self) -> None:
        blocks = {
            "24h": self._h(EDGE_NO_EDGE),
            "72h": self._h(EDGE_DETECTED),
            "168h": self._h(EDGE_DETECTED),
        }
        assert _overall_verdict(blocks) == "NO_GO"

    def test_go_on_primary_detected(self) -> None:
        blocks = {
            "24h": self._h(EDGE_POSSIBLE),
            "72h": self._h(EDGE_DETECTED),
            "168h": self._h(EDGE_POSSIBLE),
        }
        assert _overall_verdict(blocks) == "GO"

    def test_watch_partial(self) -> None:
        blocks = {
            "24h": self._h(EDGE_INSUFFICIENT),
            "72h": self._h(EDGE_POSSIBLE),
            "168h": self._h(EDGE_POSSIBLE),
        }
        assert _overall_verdict(blocks) == "WATCH"

    def test_go_all_detected(self) -> None:
        blocks = {
            "24h": self._h(EDGE_DETECTED),
            "72h": self._h(EDGE_DETECTED),
            "168h": self._h(EDGE_DETECTED),
        }
        assert _overall_verdict(blocks) == "GO"


# ---------------------------------------------------------------------------
# TestBuildReadinessReport
# ---------------------------------------------------------------------------


class TestBuildReadinessReport:
    def _db_with(self, rows: list) -> MagicMock:
        db = MagicMock()
        q = MagicMock()
        q.order_by.return_value = q
        q.all.return_value = rows
        db.query.return_value = q
        return db

    def test_report_structure(self) -> None:
        report = build_readiness_report(self._db_with([]))
        assert "generated_at" in report
        assert "n_signals_tracked" in report
        assert "overall_readiness" in report
        assert "overall_verdict" in report
        assert "edge_by_horizon" in report
        assert "horizons" in report
        assert "filter" in report

    def test_horizon_keys(self) -> None:
        report = build_readiness_report(self._db_with([]))
        for h in ["24h", "72h", "168h"]:
            assert h in report["horizons"]

    def test_empty_is_bootstrap(self) -> None:
        report = build_readiness_report(self._db_with([]))
        assert report["overall_readiness"] == READINESS_BOOTSTRAP
        assert report["overall_verdict"] == "INSUFFICIENT_DATA"

    def test_real_data_4_signals(self) -> None:
        rows = [
            _make_shadow(
                return_24h=-0.30, outcome_correct_24h=False,
                return_72h=1.07, outcome_correct_72h=True,
                return_168h=1.71, outcome_correct_168h=True,
            ),
            _make_shadow(
                return_24h=-3.45, outcome_correct_24h=False,
                return_72h=0.35, outcome_correct_72h=True,
                return_168h=6.18, outcome_correct_168h=True,
            ),
            _make_shadow(
                return_24h=-5.03, outcome_correct_24h=False,
                return_72h=-0.73, outcome_correct_72h=False,
                return_168h=4.53, outcome_correct_168h=True,
            ),
            _make_shadow(
                return_24h=-0.01, outcome_correct_24h=False,
                return_72h=1.95, outcome_correct_72h=True,
                return_168h=3.82, outcome_correct_168h=True,
            ),
        ]
        report = build_readiness_report(self._db_with(rows))
        assert report["n_signals_tracked"] == 4
        h24 = report["horizons"]["24h"]
        h72 = report["horizons"]["72h"]
        h168 = report["horizons"]["168h"]
        assert h24["win_rate"] == 0.0
        assert h72["win_rate"] == 0.75
        assert h168["win_rate"] == 1.0
        # All BOOTSTRAP (n=4 < 10)
        assert all(
            v["readiness_score"] == READINESS_BOOTSTRAP
            for v in report["horizons"].values()
        )


# ---------------------------------------------------------------------------
# TestDailySummaryMessage
# ---------------------------------------------------------------------------


class TestDailySummaryMessage:
    def _minimal_report(self) -> dict:
        return {
            "n_signals_tracked": 4,
            "overall_verdict": "WATCH",
            "overall_readiness": READINESS_BOOTSTRAP,
            "horizons": {
                "24h": {
                    "n_evaluated": 4,
                    "win_rate": 0.0,
                    "profit_factor": None,
                    "edge_status": EDGE_INSUFFICIENT,
                    "win_rate_ci_95": (0.0, 0.6),
                    "gates": {"n_ge_10": False, "n_ge_30": False, "n_ge_100": False},
                },
                "72h": {
                    "n_evaluated": 4,
                    "win_rate": 0.75,
                    "profit_factor": 4.64,
                    "edge_status": EDGE_POSSIBLE,
                    "win_rate_ci_95": (0.3, 0.97),
                    "gates": {"n_ge_10": False, "n_ge_30": False, "n_ge_100": False},
                },
                "168h": {
                    "n_evaluated": 4,
                    "win_rate": 1.0,
                    "profit_factor": None,
                    "edge_status": EDGE_POSSIBLE,
                    "win_rate_ci_95": (0.6, 1.0),
                    "gates": {"n_ge_10": False, "n_ge_30": False, "n_ge_100": False},
                },
            },
        }

    def test_message_contains_verdict(self) -> None:
        msg = build_daily_summary_message(self._minimal_report())
        assert "WATCH" in msg

    def test_message_contains_n(self) -> None:
        msg = build_daily_summary_message(self._minimal_report())
        assert "4" in msg

    def test_message_contains_all_horizons(self) -> None:
        msg = build_daily_summary_message(self._minimal_report())
        assert "24h" in msg
        assert "72h" in msg
        assert "168h" in msg

    def test_message_contains_rules_note(self) -> None:
        msg = build_daily_summary_message(self._minimal_report())
        assert "sem trades" in msg or "observa" in msg

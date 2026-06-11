"""
Basketball module smoke tests.

Tests verify:
  1. Imports — all modules load without error
  2. Shared enums are accessible from both nba.quant.models and basketball.shared.enums
  3. WNBA models define correct table names
  4. shared.settlement._pnl math is correct
  5. shared.settlement.determine_result resolves moneyline/spread/totals correctly
  6. shared.analytics_base drawdown + classify
  7. WNBA signals module is importable and _RULES is populated
  8. NBA backward compat — original imports still work
  9. NBA enums == WNBA enums (same canonical objects via shared)
"""
import pytest


# ── 1. Import smoke ────────────────────────────────────────────────────────────

def test_shared_enums_import():
    from app.modules.basketball.shared.enums import (
        BetStatus, EdgeClassification, GameStatus, MarketType, SignalDirection,
    )
    assert GameStatus.final == "final"
    assert MarketType.moneyline == "moneyline"
    assert BetStatus.pending == "pending"


def test_shared_settlement_import():
    from app.modules.basketball.shared.settlement import determine_result, pnl
    assert callable(pnl)
    assert callable(determine_result)


def test_shared_analytics_base_import():
    from app.modules.basketball.shared.analytics_base import (
        GlobalAnalytics, SetupAnalytics, classify, drawdown,
    )
    assert callable(drawdown)
    assert callable(classify)


def test_wnba_models_import():
    from app.modules.basketball.wnba.models import (
        WnbaEdgeRegistry, WnbaFeatures, WnbaGame, WnbaOdds,
        WnbaQuantBet, WnbaSignal,
    )
    assert WnbaGame.__tablename__ == "wnba_games"
    assert WnbaOdds.__tablename__ == "wnba_odds"
    assert WnbaFeatures.__tablename__ == "wnba_features"
    assert WnbaSignal.__tablename__ == "wnba_signals"
    assert WnbaQuantBet.__tablename__ == "wnba_quant_bets"
    assert WnbaEdgeRegistry.__tablename__ == "wnba_edge_registry"


def test_wnba_collector_import():
    from app.modules.basketball.wnba.collector import fetch_recent, fetch_season
    assert callable(fetch_season)
    assert callable(fetch_recent)


def test_wnba_signals_import():
    from app.modules.basketball.wnba.signals import _RULES, generate_signals
    assert len(_RULES) == 5, f"Expected 5 WNBA rules, got {len(_RULES)}"


def test_wnba_pipeline_import():
    from app.modules.basketball.wnba.pipeline import run_daily_update, run_full_pipeline
    assert callable(run_full_pipeline)
    assert callable(run_daily_update)


def test_wnba_api_import():
    from app.modules.basketball.wnba.api import router
    assert router.prefix == "/api/v1/wnba/quant"


# ── 2. Shared enums == NBA enums (backward compat) ────────────────────────────

def test_nba_backward_compat_enums():
    from app.modules.basketball.shared.enums import GameStatus as SharedGameStatus
    from app.modules.nba.quant.models import GameStatus as NbaGameStatus
    assert SharedGameStatus is NbaGameStatus, (
        "NBA GameStatus must be the same object as shared GameStatus "
        "(nba.quant.models imports from basketball.shared.enums)"
    )


def test_nba_backward_compat_imports():
    """Original NBA imports must not raise."""
    from app.modules.nba.quant.models import (  # noqa: F401
        BetStatus, EdgeClassification, GameStatus,
        MarketType, NbaEdgeRegistry, NbaFeatures,
        NbaGame, NbaOdds, NbaQuantBet, NbaSignal, SignalDirection,
    )


def test_nba_basketball_nba_shim():
    """basketball.nba shim must re-export nba.quant modules."""
    import app.modules.basketball.nba as nba_shim
    assert hasattr(nba_shim, "models")
    assert hasattr(nba_shim, "signals")
    assert hasattr(nba_shim, "pipeline")


# ── 3. PnL math ───────────────────────────────────────────────────────────────

def test_pnl_positive_odd_win():
    from app.modules.basketball.shared.settlement import pnl
    # +150 odd, 1 unit stake, win → profit = 1 * 150 / 100 = 1.5
    assert pnl(150.0, 1.0, True) == pytest.approx(1.5)


def test_pnl_negative_odd_win():
    from app.modules.basketball.shared.settlement import pnl
    # -110 odd, 1 unit stake, win → profit = 1 * 100 / 110 ≈ 0.9091
    assert pnl(-110.0, 1.0, True) == pytest.approx(100 / 110, rel=1e-3)


def test_pnl_loss():
    from app.modules.basketball.shared.settlement import pnl
    assert pnl(-110.0, 1.0, False) == pytest.approx(-1.0)


def test_pnl_invalid_odd_between_minus100_and_zero():
    from app.modules.basketball.shared.settlement import pnl
    with pytest.raises(ValueError, match="Invalid American odd"):
        pnl(-50.0, 1.0, True)


def test_pnl_invalid_odd_zero():
    from app.modules.basketball.shared.settlement import pnl
    with pytest.raises(ValueError):
        pnl(0.0, 1.0, True)


# ── 4. determine_result ────────────────────────────────────────────────────────

class _FakeGame:
    def __init__(self, hs, as_, status="final"):
        from app.modules.basketball.shared.enums import GameStatus
        self.home_score = hs
        self.away_score = as_
        self.status = GameStatus(status)


class _FakeSignal:
    def __init__(self, market_type, direction, line=None):
        from app.modules.basketball.shared.enums import MarketType, SignalDirection
        self.market_type = MarketType(market_type)
        self.signal_direction = SignalDirection(direction)
        self.line = line


def test_determine_result_moneyline_home_win():
    from app.modules.basketball.shared.enums import BetStatus
    from app.modules.basketball.shared.settlement import determine_result
    game = _FakeGame(85, 78)
    signal = _FakeSignal("moneyline", "home")
    assert determine_result(game, signal) == BetStatus.won


def test_determine_result_moneyline_home_loss():
    from app.modules.basketball.shared.enums import BetStatus
    from app.modules.basketball.shared.settlement import determine_result
    game = _FakeGame(78, 85)
    signal = _FakeSignal("moneyline", "home")
    assert determine_result(game, signal) == BetStatus.lost


def test_determine_result_spread_home_cover():
    from app.modules.basketball.shared.enums import BetStatus
    from app.modules.basketball.shared.settlement import determine_result
    # Home +3.5, home wins by 1: 78+3.5-77 = 4.5 > 0 → won
    game = _FakeGame(78, 77)
    signal = _FakeSignal("spread", "home", line=3.5)
    assert determine_result(game, signal) == BetStatus.won


def test_determine_result_spread_void():
    from app.modules.basketball.shared.enums import BetStatus
    from app.modules.basketball.shared.settlement import determine_result
    # Home -3, home wins by exactly 3: 83+(-3)-80 = 0 → void
    game = _FakeGame(83, 80)
    signal = _FakeSignal("spread", "home", line=-3.0)
    assert determine_result(game, signal) == BetStatus.void


def test_determine_result_totals_over_win():
    from app.modules.basketball.shared.enums import BetStatus
    from app.modules.basketball.shared.settlement import determine_result
    # Total 160, line 155.5 → over wins
    game = _FakeGame(82, 78)  # total 160
    signal = _FakeSignal("totals", "over", line=155.5)
    assert determine_result(game, signal) == BetStatus.won


def test_determine_result_not_final():
    from app.modules.basketball.shared.settlement import determine_result
    game = _FakeGame(82, 78, status="live")
    signal = _FakeSignal("moneyline", "home")
    assert determine_result(game, signal) is None


# ── 5. Analytics base ─────────────────────────────────────────────────────────

def test_drawdown_empty():
    from app.modules.basketball.shared.analytics_base import drawdown
    assert drawdown([]) == 0.0


def test_drawdown_basic():
    from app.modules.basketball.shared.analytics_base import drawdown
    # peak at 3, then -1 each step: drawdown should be 2
    assert drawdown([1.0, 1.0, 1.0, -1.0, -1.0]) == pytest.approx(2.0)


def test_classify_neutral_low_sample():
    from app.modules.basketball.shared.analytics_base import classify
    from app.modules.basketball.shared.enums import EdgeClassification
    # Below MIN_BETS_FOR_CLASSIFICATION → always neutral
    assert classify(roi=20.0, total=50) == EdgeClassification.neutral


def test_classify_profitable():
    from app.modules.basketball.shared.analytics_base import classify
    from app.modules.basketball.shared.enums import EdgeClassification
    assert classify(roi=6.0, total=300) == EdgeClassification.profitable


def test_classify_losing():
    from app.modules.basketball.shared.analytics_base import classify
    from app.modules.basketball.shared.enums import EdgeClassification
    assert classify(roi=-6.0, total=300) == EdgeClassification.losing


# ── 6. WNBA season windows ────────────────────────────────────────────────────

def test_wnba_season_windows_defined():
    from app.modules.basketball.wnba.collector import _WNBA_SEASON_WINDOWS
    for season in [2022, 2023, 2024, 2025]:
        assert season in _WNBA_SEASON_WINDOWS, f"Missing WNBA season {season}"


def test_wnba_odds_collector_sport_key():
    from app.modules.basketball.wnba.odds_collector import _SPORT
    assert _SPORT == "basketball_wnba"


# ── 7. Classification metrics mapping (bugfix: lowercase enum values) ─────────

def test_classification_metrics_map_profitable():
    _MAP = {"profitable": 1, "neutral": 0, "losing": -1}
    from app.modules.basketball.shared.enums import EdgeClassification
    assert _MAP.get(EdgeClassification.profitable.value) == 1


def test_classification_metrics_map_neutral():
    _MAP = {"profitable": 1, "neutral": 0, "losing": -1}
    from app.modules.basketball.shared.enums import EdgeClassification
    assert _MAP.get(EdgeClassification.neutral.value) == 0


def test_classification_metrics_map_losing():
    _MAP = {"profitable": 1, "neutral": 0, "losing": -1}
    from app.modules.basketball.shared.enums import EdgeClassification
    assert _MAP.get(EdgeClassification.losing.value) == -1


# ── 8. Pipeline abort on fetch_recent failure with no historical data ─────────

def test_pipeline_abort_pattern():
    """Verify the abort condition logic: errors + games_ingested == 0 → early return."""
    from app.modules.basketball.wnba.pipeline import PipelineResult
    from datetime import datetime, timezone
    result = PipelineResult(started_at=datetime.now(timezone.utc))
    result.errors.append("fetch_recent: timeout")
    # Simulates the abort condition: no historical data ingested
    assert result.games_ingested == 0
    assert not result.ok  # has errors → ok=False

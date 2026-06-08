"""
Unit tests for NBA Quant Engine.
No database required — pure logic tests for signals, paper betting, and analytics.
"""
import types
import uuid
from datetime import datetime, timezone

import pytest

from app.modules.nba.quant.analytics import _classify, _drawdown
from app.modules.nba.quant.models import (
    BetStatus,
    EdgeClassification,
    GameStatus,
    MarketType,
    SignalDirection,
)
from app.modules.nba.quant.paper_betting import _determine_result, _pnl
from app.modules.nba.quant.signals import (
    _back_to_back_fade_v1,
    _home_dog_v1,
    _pace_over_v1,
    _rest_advantage_v1,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _game(home="Lakers", away="Celtics", status=GameStatus.scheduled):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        home_team=home,
        away_team=away,
        home_score=None,
        away_score=None,
        status=status,
        game_date=datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc),
        season=2024,
    )


def _feat(**kwargs):
    return types.SimpleNamespace(
        home_rest_days=kwargs.get("home_rest_days", 2),
        away_rest_days=kwargs.get("away_rest_days", 2),
        home_back_to_back=kwargs.get("home_back_to_back", False),
        away_back_to_back=kwargs.get("away_back_to_back", False),
        home_last5_wins=kwargs.get("home_last5_wins", 3),
        home_last5_games=kwargs.get("home_last5_games", 5),
        away_last5_wins=kwargs.get("away_last5_wins", 3),
        away_last5_games=kwargs.get("away_last5_games", 5),
        home_last10_wins=kwargs.get("home_last10_wins", 6),
        home_last10_games=kwargs.get("home_last10_games", 10),
        away_last10_wins=kwargs.get("away_last10_wins", 6),
        away_last10_games=kwargs.get("away_last10_games", 10),
        home_pace=kwargs.get("home_pace", 215.0),
        away_pace=kwargs.get("away_pace", 215.0),
        home_off_rtg=kwargs.get("home_off_rtg", 115.0),
        away_off_rtg=kwargs.get("away_off_rtg", 112.0),
        home_def_rtg=kwargs.get("home_def_rtg", 108.0),
        away_def_rtg=kwargs.get("away_def_rtg", 110.0),
    )


def _odds(market: MarketType, selection: str, odd: float, line: float | None = None):
    return types.SimpleNamespace(
        market_type=market,
        selection=selection,
        odd=odd,
        line=line,
    )


def _signal(
    direction: SignalDirection,
    market: MarketType,
    line: float | None,
    odd: float,
    setup: str = "TEST",
):
    return types.SimpleNamespace(
        signal_direction=direction,
        market_type=market,
        line=line,
        odd=odd,
        setup_name=setup,
        selection="Test",
    )


# ── HOME_DOG_V1 ───────────────────────────────────────────────────────────────

def test_home_dog_fires_when_underdog():
    g = _game()
    f = _feat(home_rest_days=2, away_rest_days=1, home_last5_wins=3)
    odds = [_odds(MarketType.moneyline, "Lakers", 150.0)]
    r = _home_dog_v1(g, f, odds)
    assert r is not None
    assert r.setup_name == "HOME_DOG_V1"
    assert r.odd == 150.0


def test_home_dog_no_signal_when_favorite():
    g = _game()
    f = _feat(home_rest_days=2, away_rest_days=1, home_last5_wins=3)
    odds = [_odds(MarketType.moneyline, "Lakers", -150.0)]
    assert _home_dog_v1(g, f, odds) is None


def test_home_dog_no_signal_when_bad_form():
    g = _game()
    f = _feat(home_rest_days=2, away_rest_days=1, home_last5_wins=1)
    odds = [_odds(MarketType.moneyline, "Lakers", 130.0)]
    assert _home_dog_v1(g, f, odds) is None


def test_home_dog_no_signal_when_rest_disadvantage():
    g = _game()
    f = _feat(home_rest_days=1, away_rest_days=3, home_last5_wins=3)
    odds = [_odds(MarketType.moneyline, "Lakers", 130.0)]
    assert _home_dog_v1(g, f, odds) is None


# ── REST_ADVANTAGE_V1 ─────────────────────────────────────────────────────────

def test_rest_advantage_fires_on_2_day_diff():
    g = _game()
    f = _feat(home_rest_days=3, away_rest_days=1)
    odds = [_odds(MarketType.spread, "Lakers", -110.0, line=-3.5)]
    r = _rest_advantage_v1(g, f, odds)
    assert r is not None
    assert r.setup_name == "REST_ADVANTAGE_V1"


def test_rest_advantage_no_signal_on_equal_rest():
    g = _game()
    f = _feat(home_rest_days=2, away_rest_days=2)
    r = _rest_advantage_v1(g, f, [])
    assert r is None


def test_rest_advantage_no_signal_on_1_day_diff():
    g = _game()
    f = _feat(home_rest_days=2, away_rest_days=1)
    r = _rest_advantage_v1(g, f, [])
    assert r is None


# ── BACK_TO_BACK_FADE_V1 ──────────────────────────────────────────────────────

def test_b2b_fade_fires_when_away_on_b2b():
    g = _game()
    f = _feat(away_back_to_back=True, home_back_to_back=False)
    odds = [_odds(MarketType.moneyline, "Lakers", -120.0)]
    r = _back_to_back_fade_v1(g, f, odds)
    assert r is not None
    assert r.setup_name == "BACK_TO_BACK_FADE_V1"


def test_b2b_fade_no_signal_when_both_b2b():
    g = _game()
    f = _feat(away_back_to_back=True, home_back_to_back=True)
    r = _back_to_back_fade_v1(g, f, [])
    assert r is None


def test_b2b_fade_no_signal_when_home_only_b2b():
    g = _game()
    f = _feat(away_back_to_back=False, home_back_to_back=True)
    r = _back_to_back_fade_v1(g, f, [])
    assert r is None


# ── PACE_OVER_V1 ──────────────────────────────────────────────────────────────

def test_pace_over_fires_above_threshold():
    g = _game()
    f = _feat(home_pace=228.0, away_pace=225.0)
    odds = [_odds(MarketType.totals, "over 226.5", -108.0, line=226.5)]
    r = _pace_over_v1(g, f, odds)
    assert r is not None
    assert r.setup_name == "PACE_OVER_V1"
    assert r.direction == SignalDirection.over


def test_pace_over_no_signal_below_threshold():
    g = _game()
    f = _feat(home_pace=210.0, away_pace=208.0)
    r = _pace_over_v1(g, f, [])
    assert r is None


def test_pace_over_no_signal_when_pace_missing():
    g = _game()
    f = _feat(home_pace=None, away_pace=None)
    r = _pace_over_v1(g, f, [])
    assert r is None


# ── Paper Betting ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("odd,stake,won,expected", [
    (150.0, 1.0, True, 1.5),
    (-110.0, 1.0, True, pytest.approx(0.9090, abs=1e-3)),
    (-150.0, 1.0, True, pytest.approx(0.6666, abs=1e-3)),
    (-110.0, 1.0, False, -1.0),
])
def test_pnl_calculation(odd, stake, won, expected):
    assert _pnl(odd, stake, won) == expected


def test_determine_result_moneyline_home_win():
    g = _game(status=GameStatus.final)
    g.home_score, g.away_score = 110, 105
    s = _signal(SignalDirection.home, MarketType.moneyline, None, -110.0)
    assert _determine_result(g, s) == BetStatus.won


def test_determine_result_moneyline_home_loss():
    g = _game(status=GameStatus.final)
    g.home_score, g.away_score = 100, 115
    s = _signal(SignalDirection.home, MarketType.moneyline, None, 130.0)
    assert _determine_result(g, s) == BetStatus.lost


def test_determine_result_spread_home_covers():
    g = _game(status=GameStatus.final)
    g.home_score, g.away_score = 112, 105  # margin = 7, spread = -5.5, so 7-5.5=1.5 > 0
    s = _signal(SignalDirection.home, MarketType.spread, -5.5, -110.0)
    assert _determine_result(g, s) == BetStatus.won


def test_determine_result_spread_push():
    g = _game(status=GameStatus.final)
    g.home_score, g.away_score = 105, 100  # margin = 5, spread = -5.0 → 5-5=0
    s = _signal(SignalDirection.home, MarketType.spread, -5.0, -110.0)
    assert _determine_result(g, s) == BetStatus.void


def test_determine_result_over_wins():
    g = _game(status=GameStatus.final)
    g.home_score, g.away_score = 115, 112  # total = 227 > 224.5
    s = _signal(SignalDirection.over, MarketType.totals, 224.5, -108.0)
    assert _determine_result(g, s) == BetStatus.won


def test_determine_result_over_loses():
    g = _game(status=GameStatus.final)
    g.home_score, g.away_score = 108, 104  # total = 212 < 224.5
    s = _signal(SignalDirection.over, MarketType.totals, 224.5, -108.0)
    assert _determine_result(g, s) == BetStatus.lost


def test_determine_result_no_result_if_not_final():
    g = _game(status=GameStatus.scheduled)
    g.home_score, g.away_score = None, None
    s = _signal(SignalDirection.home, MarketType.moneyline, None, -110.0)
    assert _determine_result(g, s) is None


# ── Analytics ─────────────────────────────────────────────────────────────────

def test_drawdown_flat():
    assert _drawdown([1.0, 1.0, 1.0]) == 0.0


def test_drawdown_simple():
    assert _drawdown([1.0, -2.0, 1.0]) == pytest.approx(2.0)


def test_drawdown_empty():
    assert _drawdown([]) == 0.0


def test_classify_profitable():
    assert _classify(6.0, 30) == EdgeClassification.profitable


def test_classify_losing():
    assert _classify(-6.0, 30) == EdgeClassification.losing


def test_classify_neutral_insufficient_bets():
    assert _classify(10.0, 10) == EdgeClassification.neutral


def test_classify_neutral_in_range():
    assert _classify(2.0, 30) == EdgeClassification.neutral

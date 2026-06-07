"""
Unit tests for the NBA pick parser.
No database required — pure function tests.
"""
import pytest

from app.modules.nba.models import PickType
from app.modules.nba.parser import parse_pick


@pytest.mark.parametrize(
    "raw, expected_type, expected_team, expected_line, expected_odd",
    [
        # Moneyline
        ("Lakers ML -150", PickType.moneyline, "Lakers", None, -150.0),
        ("Celtics ML +130", PickType.moneyline, "Celtics", None, 130.0),
        # Spread
        ("Warriors -5.5 (-110)", PickType.spread, "Warriors", -5.5, -110.0),
        ("Nets +3.5 (+105)", PickType.spread, "Nets", 3.5, 105.0),
        # Total
        ("Over 224.5 -108", PickType.total, "Over 224.5", 224.5, -108.0),
        ("Under 210.0 -112", PickType.total, "Under 210.0", 210.0, -112.0),
        ("O 228.5 +100", PickType.total, "Over 228.5", 228.5, 100.0),
        ("U 215 -115", PickType.total, "Under 215.0", 215.0, -115.0),
        # Player props
        ("LeBron James Over 25.5 points -115", PickType.player_prop, None, 25.5, -115.0),
        ("Stephen Curry Under 4.5 assists +110", PickType.player_prop, None, 4.5, 110.0),
    ],
)
def test_parse_pick_types(raw, expected_type, expected_team, expected_line, expected_odd):
    result = parse_pick(raw)
    assert result.pick_type == expected_type
    if expected_team is not None:
        assert result.team == expected_team
    if expected_line is not None:
        assert result.line == pytest.approx(expected_line)
    assert result.odd == pytest.approx(expected_odd)


def test_parse_error_returns_error_status():
    result = parse_pick("gibberish with no structure")
    assert result.parse_status in ("error", "fallback")


def test_parse_preserves_raw_text():
    raw = "Lakers -3.5 (-110)"
    result = parse_pick(raw)
    assert result.raw_text == raw


def test_player_prop_captures_player_name():
    result = parse_pick("Kevin Durant Over 28.5 points -120")
    assert result.pick_type == PickType.player_prop
    assert result.player == "Kevin Durant"
    assert result.line == pytest.approx(28.5)


def test_total_over_under_labels():
    over = parse_pick("Over 220 -110")
    under = parse_pick("Under 220 -110")
    assert over.team and over.team.startswith("Over")
    assert under.team and under.team.startswith("Under")

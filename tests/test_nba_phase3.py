"""
Tests for NBA Quant Phase 3:
  - OddsCollector (The Odds API integration)
  - OutOfSample validation (metrics computation)
  - TelegramAlerts (format + send logic)
  - Pipeline Phase 3 fields (odds_upserted, alerts_sent)

All HTTP calls mocked — no real network, no real DB.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_mock_db() -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.all.return_value = []
    return db


def _make_signal(setup="BACK_TO_BACK_FADE_V1", odd=-110.0, line=None):
    s = MagicMock()
    s.setup_name = setup
    s.odd = odd
    s.line = line
    s.market_type = "moneyline"
    s.signal_direction = "home"
    s.selection = "Los Angeles Lakers"
    s.rationale = "Away on B2B, home rested (2d)"
    s.confidence = 1.0
    s.telegram_sent_at = None
    return s


def _make_game(home="Los Angeles Lakers", away="Boston Celtics", home_score=110, away_score=105):
    g = MagicMock()
    g.home_team = home
    g.away_team = away
    g.home_score = home_score
    g.away_score = away_score
    g.game_date = datetime(2024, 1, 15, 23, 30, tzinfo=timezone.utc)
    g.season = 2024
    return g


def _make_features(home_rest=2, away_rest=1, h_b2b=False, a_b2b=True, h5w=3, h5g=5):
    f = MagicMock()
    f.home_rest_days = home_rest
    f.away_rest_days = away_rest
    f.home_back_to_back = h_b2b
    f.away_back_to_back = a_b2b
    f.home_last5_wins = h5w
    f.home_last5_games = h5g
    return f


def _make_bet(status, pnl, stake=1.0):
    from app.modules.nba.quant.models import BetStatus
    b = MagicMock()
    b.status = BetStatus.won if status == "won" else BetStatus.lost
    b.pnl = pnl
    b.stake = stake
    return b


# ── OddsCollector: blocked without API key ────────────────────────────────────

def test_fetch_upcoming_odds_blocked_no_key():
    """Without API key, fetch_upcoming_odds returns blocked result."""
    import importlib
    saved = os.environ.pop("THE_ODDS_API_KEY", None)
    try:
        import app.modules.nba.quant.odds_collector as mod
        importlib.reload(mod)
        db = _make_mock_db()
        result = mod.fetch_upcoming_odds(db)
        assert result.blocked is True
        assert "THE_ODDS_API_KEY" in result.blocked_reason
        assert result.odds_upserted == 0
    finally:
        if saved:
            os.environ["THE_ODDS_API_KEY"] = saved
        importlib.reload(mod)


def test_fetch_historical_odds_blocked_no_key():
    """Without API key, fetch_historical_odds returns blocked result."""
    import importlib
    saved = os.environ.pop("THE_ODDS_API_KEY", None)
    try:
        import app.modules.nba.quant.odds_collector as mod
        importlib.reload(mod)
        db = _make_mock_db()
        result = mod.fetch_historical_odds(db, datetime(2023, 10, 24, tzinfo=timezone.utc))
        assert result.blocked is True
    finally:
        if saved:
            os.environ["THE_ODDS_API_KEY"] = saved
        importlib.reload(mod)


# ── OddsCollector: event parsing ──────────────────────────────────────────────

def _make_odds_event(
    home="Los Angeles Lakers",
    away="Boston Celtics",
    commence="2024-01-15T23:30:00Z",
    h2h_home=None,
    h2h_away=None,
    spread_home=None,
    total_over=None,
):
    bookmaker = {"key": "draftkings", "markets": []}

    if h2h_home is not None or h2h_away is not None:
        bookmaker["markets"].append({
            "key": "h2h",
            "outcomes": [
                {"name": home, "price": h2h_home or -150},
                {"name": away, "price": h2h_away or 130},
            ],
        })

    if spread_home is not None:
        bookmaker["markets"].append({
            "key": "spreads",
            "outcomes": [
                {"name": home, "price": -110, "point": spread_home},
                {"name": away, "price": -110, "point": -spread_home},
            ],
        })

    if total_over is not None:
        bookmaker["markets"].append({
            "key": "totals",
            "outcomes": [
                {"name": "Over", "price": -110, "point": total_over},
                {"name": "Under", "price": -110, "point": total_over},
            ],
        })

    return {
        "id": "abc123",
        "home_team": home,
        "away_team": away,
        "commence_time": commence,
        "bookmakers": [bookmaker],
    }


def test_process_event_moneyline_upserted():
    """Moneyline odds are upserted when game matches."""
    from app.modules.nba.quant.odds_collector import OddsCollectResult, _process_event

    game = _make_game()
    db = _make_mock_db()
    db.query.return_value.filter.return_value.first.return_value = game

    result = OddsCollectResult()
    event = _make_odds_event(h2h_home=-150, h2h_away=130)

    with patch("app.modules.nba.quant.odds_collector._match_game", return_value=game):
        _process_event(db, event, result)

    assert result.games_matched == 1
    assert result.odds_upserted == 2  # home ML + away ML


def test_process_event_spread_and_total():
    """Spread + totals odds are parsed correctly."""
    from app.modules.nba.quant.odds_collector import OddsCollectResult, _process_event

    game = _make_game()
    result = OddsCollectResult()
    event = _make_odds_event(spread_home=-4.5, total_over=220.5)

    with patch("app.modules.nba.quant.odds_collector._match_game", return_value=game):
        _process_event(db=_make_mock_db(), event=event, result=result)

    assert result.games_matched == 1
    assert result.odds_upserted == 4  # 2 spread + 2 totals


def test_process_event_no_game_match():
    """Events with no matching game are recorded as unmatched."""
    from app.modules.nba.quant.odds_collector import OddsCollectResult, _process_event

    result = OddsCollectResult()
    event = _make_odds_event(h2h_home=-150, h2h_away=130)

    with patch("app.modules.nba.quant.odds_collector._match_game", return_value=None):
        _process_event(db=_make_mock_db(), event=event, result=result)

    assert result.games_matched == 0
    assert len(result.games_unmatched) == 1


def test_fetch_upcoming_odds_http_error():
    """HTTP errors from The Odds API are captured in result.errors."""
    import importlib

    import httpx

    with patch.dict(os.environ, {"THE_ODDS_API_KEY": "test_key"}):
        import app.modules.nba.quant.odds_collector as mod
        importlib.reload(mod)

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "rate limit exceeded"
        mock_resp.headers = {}

        with patch("app.modules.nba.quant.odds_collector.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.side_effect = (
                httpx.HTTPStatusError("rate limit", request=MagicMock(), response=mock_resp)
            )
            result = mod.fetch_upcoming_odds(_make_mock_db())

        assert len(result.errors) > 0
        assert not result.blocked

    importlib.reload(mod)


def test_fetch_historical_odds_403_returns_blocked():
    """403 from historical endpoint → blocked=True (paid plan required)."""
    import importlib

    import httpx

    with patch.dict(os.environ, {"THE_ODDS_API_KEY": "free_key"}):
        import app.modules.nba.quant.odds_collector as mod
        importlib.reload(mod)

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        mock_resp.headers = {}

        with patch("app.modules.nba.quant.odds_collector.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.side_effect = (
                httpx.HTTPStatusError("forbidden", request=MagicMock(), response=mock_resp)
            )
            result = mod.fetch_historical_odds(
                _make_mock_db(), datetime(2023, 10, 24, tzinfo=timezone.utc)
            )

        assert result.blocked is True
        assert "paid plan" in result.blocked_reason.lower()

    importlib.reload(mod)


# ── OddsCollector: _match_game ────────────────────────────────────────────────

def test_match_game_found():
    """_match_game returns game when team names and date match."""
    from app.modules.nba.quant.odds_collector import _match_game

    game = _make_game()
    db = _make_mock_db()
    db.query.return_value.filter.return_value.first.return_value = game

    result = _match_game(db, "Los Angeles Lakers", "Boston Celtics", "2024-01-15T23:30:00Z")
    assert result is game


def test_match_game_invalid_date():
    """Invalid commence_time returns None."""
    from app.modules.nba.quant.odds_collector import _match_game

    result = _match_game(_make_mock_db(), "Lakers", "Celtics", "not-a-date")
    assert result is None


# ── OutOfSample: _compute_metrics ─────────────────────────────────────────────

def test_compute_metrics_empty():
    from app.modules.nba.quant.out_of_sample import _compute_metrics

    result = _compute_metrics([], [2024])
    assert result.sample_size == 0
    assert result.roi == 0.0
    assert result.win_rate == 0.0


def test_compute_metrics_all_wins():
    from app.modules.nba.quant.out_of_sample import _compute_metrics

    bets = [_make_bet("won", 0.909) for _ in range(10)]
    result = _compute_metrics(bets, [2024])
    assert result.wins == 10
    assert result.losses == 0
    assert result.win_rate == pytest.approx(1.0)
    assert result.roi > 0
    assert result.profit_factor == float("inf")
    assert result.max_drawdown == 0.0


def test_compute_metrics_mixed_bets():
    from app.modules.nba.quant.out_of_sample import _compute_metrics

    # 6 wins at +0.909, 4 losses at -1.0 → net positive
    wins = [_make_bet("won", 0.909) for _ in range(6)]
    losses = [_make_bet("lost", -1.0) for _ in range(4)]
    result = _compute_metrics(wins + losses, [2024])

    assert result.wins == 6
    assert result.losses == 4
    assert result.win_rate == pytest.approx(0.6)
    assert result.roi > 0
    assert result.profit_factor > 1.0
    assert result.sharpe != 0.0


def test_compute_metrics_sharpe_zero_when_all_identical():
    """When all returns are identical (zero variance), Sharpe = 0."""
    from app.modules.nba.quant.out_of_sample import _compute_metrics

    losses = [_make_bet("lost", -1.0) for _ in range(10)]
    result = _compute_metrics(losses, [2024])
    assert result.sharpe == 0.0
    assert result.roi < 0


def test_compute_metrics_max_drawdown():
    from app.modules.nba.quant.out_of_sample import _compute_metrics

    # +1, +1, -3, +1 → cumulative [1, 2, -1, 0] → peak=2, trough=-1 → dd=3
    bets = [
        _make_bet("won", 1.0),
        _make_bet("won", 1.0),
        _make_bet("lost", -3.0),
        _make_bet("won", 1.0),
    ]
    result = _compute_metrics(bets, [2024])
    assert result.max_drawdown == pytest.approx(3.0)


# ── OutOfSample: verdict classification ──────────────────────────────────────

def test_verdict_edge_confirmed():
    from app.modules.nba.quant.out_of_sample import WindowMetrics, _verdict

    train = WindowMetrics(seasons=[2022, 2023], sample_size=200, roi=8.0, win_rate=0.56)
    test = WindowMetrics(seasons=[2024], sample_size=80, roi=6.0, win_rate=0.55)
    verdict, notes = _verdict(train, test)
    assert verdict == "EDGE_CONFIRMED"


def test_verdict_edge_degraded():
    from app.modules.nba.quant.out_of_sample import WindowMetrics, _verdict

    train = WindowMetrics(seasons=[2022, 2023], sample_size=200, roi=8.0, win_rate=0.56)
    test = WindowMetrics(seasons=[2024], sample_size=80, roi=-3.0, win_rate=0.50)
    verdict, notes = _verdict(train, test)
    assert verdict == "EDGE_DEGRADED"
    assert any("overfitting" in n for n in notes)


def test_verdict_no_edge():
    from app.modules.nba.quant.out_of_sample import WindowMetrics, _verdict

    train = WindowMetrics(seasons=[2022, 2023], sample_size=200, roi=-5.0, win_rate=0.49)
    test = WindowMetrics(seasons=[2024], sample_size=80, roi=-4.0, win_rate=0.48)
    verdict, _ = _verdict(train, test)
    assert verdict == "NO_EDGE"


def test_verdict_small_sample_note():
    from app.modules.nba.quant.out_of_sample import WindowMetrics, _verdict

    train = WindowMetrics(seasons=[2022, 2023], sample_size=100, roi=5.0, win_rate=0.54)
    test = WindowMetrics(seasons=[2024], sample_size=10, roi=3.0, win_rate=0.55)
    _, notes = _verdict(train, test)
    assert any("small" in n.lower() for n in notes)


def test_verdict_marginal_oos_positive_but_low_winrate():
    from app.modules.nba.quant.out_of_sample import WindowMetrics, _verdict

    train = WindowMetrics(seasons=[2022, 2023], sample_size=200, roi=5.0, win_rate=0.54)
    # OOS: ROI positive but win_rate below vig breakeven
    test = WindowMetrics(seasons=[2024], sample_size=50, roi=1.0, win_rate=0.51)
    verdict, _ = _verdict(train, test)
    assert verdict == "EDGE_MARGINAL"


# ── OutOfSample: run_oos_validation integration ───────────────────────────────

def test_run_oos_validation_empty_db():
    """With no bets in DB, all setups return NO_EDGE (or EDGE_MARGINAL with sample=0)."""
    from app.modules.nba.quant.out_of_sample import run_oos_validation

    db = _make_mock_db()
    # Return empty bets for all queries
    # oos queries return empty lists by default via MagicMock

    results = run_oos_validation(db, train_seasons=[2022, 2023], test_seasons=[2024])
    assert len(results) == 4
    for r in results:
        assert r.verdict in ("NO_EDGE", "EDGE_MARGINAL")
        assert r.train.sample_size == 0
        assert r.test.sample_size == 0


def test_run_oos_validation_custom_setups():
    from app.modules.nba.quant.out_of_sample import run_oos_validation

    db = _make_mock_db()
    # oos queries return empty lists by default via MagicMock

    results = run_oos_validation(
        db,
        train_seasons=[2022],
        test_seasons=[2023],
        setups=["BACK_TO_BACK_FADE_V1"],
    )
    assert len(results) == 1
    assert results[0].setup_name == "BACK_TO_BACK_FADE_V1"


# ── TelegramAlerts: format ────────────────────────────────────────────────────

def test_format_b2b_alert_contains_key_fields():
    from app.modules.nba.quant.telegram_alerts import format_b2b_alert

    signal = _make_signal()
    game = _make_game()
    features = _make_features()

    text = format_b2b_alert(signal, game, features)

    assert "BACK_TO_BACK_FADE_V1" in text
    assert "Los Angeles Lakers" in text
    assert "Boston Celtics" in text
    assert "aposta real" in text or "Observation" in text
    assert "Away on B2B" in text or "B2B" in text


def test_format_b2b_alert_positive_odd():
    from app.modules.nba.quant.telegram_alerts import format_b2b_alert

    signal = _make_signal(odd=120.0)
    game = _make_game()

    text = format_b2b_alert(signal, game)
    assert "+120" in text


def test_format_b2b_alert_negative_odd():
    from app.modules.nba.quant.telegram_alerts import format_b2b_alert

    signal = _make_signal(odd=-150.0)
    game = _make_game()

    text = format_b2b_alert(signal, game)
    assert "-150" in text


def test_format_b2b_alert_no_features():
    """Alert without features should still format without error."""
    from app.modules.nba.quant.telegram_alerts import format_b2b_alert

    signal = _make_signal()
    game = _make_game()
    text = format_b2b_alert(signal, game, features=None)
    assert "BACK_TO_BACK_FADE_V1" in text


# ── TelegramAlerts: send logic ────────────────────────────────────────────────

def test_send_alert_skipped_when_disabled():
    import importlib
    with patch.dict(os.environ, {"TELEGRAM_ENABLED": "false"}):
        import app.modules.nba.quant.telegram_alerts as mod
        importlib.reload(mod)
        result = mod.send_alert("test message")
        assert result is False
    importlib.reload(mod)


def test_send_alert_skipped_when_no_token():
    import importlib
    env = {"TELEGRAM_ENABLED": "true", "TELEGRAM_CHAT_ID": "123"}
    env.pop("TELEGRAM_BOT_TOKEN", None)
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        import app.modules.nba.quant.telegram_alerts as mod
        importlib.reload(mod)
        result = mod.send_alert("test")
        assert result is False
    importlib.reload(mod)


def test_send_alert_success():
    import importlib
    env = {
        "TELEGRAM_ENABLED": "true",
        "ENABLE_NBA_TELEGRAM_SIMULATIONS": "true",
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "TELEGRAM_CHAT_ID": "456",
    }
    with patch.dict(os.environ, env):
        import app.modules.nba.quant.telegram_alerts as mod
        importlib.reload(mod)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}

        with patch("app.modules.nba.quant.telegram_alerts.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_resp
            result = mod.send_alert("test message")

        assert result is True

    importlib.reload(mod)


def test_send_alert_http_error_returns_false():
    import importlib

    import httpx

    env = {
        "TELEGRAM_ENABLED": "true",
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "TELEGRAM_CHAT_ID": "456",
    }
    with patch.dict(os.environ, env):
        import app.modules.nba.quant.telegram_alerts as mod
        importlib.reload(mod)

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch("app.modules.nba.quant.telegram_alerts.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = (
                httpx.HTTPStatusError("unauth", request=MagicMock(), response=mock_resp)
            )
            result = mod.send_alert("test message")

        assert result is False

    importlib.reload(mod)


def test_send_signal_alert_skips_when_already_sent():
    """Signals already sent (telegram_sent_at set) are not resent."""
    from app.modules.nba.quant.telegram_alerts import send_signal_alert
    from datetime import datetime, timezone

    signal = _make_signal(setup="PACE_OVER_V1")
    signal.telegram_sent_at = datetime.now(timezone.utc)
    game = _make_game()
    result = send_signal_alert(signal, game)
    assert result is False


def test_validate_config_not_configured():
    import importlib
    saved = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    saved_enabled = os.environ.pop("TELEGRAM_ENABLED", None)
    try:
        import app.modules.nba.quant.telegram_alerts as mod
        importlib.reload(mod)
        config = mod.validate_config()
        assert config["configured"] is False
    finally:
        if saved:
            os.environ["TELEGRAM_BOT_TOKEN"] = saved
        if saved_enabled:
            os.environ["TELEGRAM_ENABLED"] = saved_enabled
        importlib.reload(mod)


# ── Pipeline Phase 3 fields ───────────────────────────────────────────────────

_PM = "app.modules.nba.quant.pipeline"


def _make_pipeline_mocks_p3(**overrides):
    defaults = {
        "fetch_season": MagicMock(return_value=10),
        "fetch_recent": MagicMock(return_value=2),
        "fetch_upcoming_odds": MagicMock(
            return_value=MagicMock(odds_upserted=5, blocked=False, errors=[])
        ),
        "compute_all_pending": MagicMock(return_value=8),
        "settle_all_pending": MagicMock(return_value=3),
        "refresh_edge_registry": MagicMock(
            return_value=[MagicMock(), MagicMock()]
        ),
        "nba_q_pipeline_runs_total": MagicMock(),
        "nba_q_pipeline_duration_seconds": MagicMock(),
        "_run_all_games_with_alerts": MagicMock(
            return_value={"signals": 5, "alerts": 2}
        ),
    }
    defaults.update(overrides)
    return defaults


def test_pipeline_result_includes_odds_fields():
    from app.modules.nba.quant.pipeline import run_full_pipeline

    mocks = _make_pipeline_mocks_p3()
    db = MagicMock()
    with patch.multiple(_PM, **mocks):
        result = run_full_pipeline(db, skip_historical=True)

    assert result.odds_upserted == 5
    assert result.odds_blocked is False
    assert result.alerts_sent == 2


def test_pipeline_odds_blocked_does_not_fail_pipeline():
    from app.modules.nba.quant.pipeline import run_full_pipeline

    mocks = _make_pipeline_mocks_p3(
        fetch_upcoming_odds=MagicMock(
            return_value=MagicMock(
                odds_upserted=0,
                blocked=True,
                blocked_reason="No key",
                errors=[],
            )
        )
    )
    db = MagicMock()
    with patch.multiple(_PM, **mocks):
        result = run_full_pipeline(db, skip_historical=True)

    assert result.odds_blocked is True
    assert result.ok is True  # pipeline not failed by odds block
    assert result.errors == []


def test_pipeline_signals_and_alerts_counted():
    from app.modules.nba.quant.pipeline import run_full_pipeline

    mocks = _make_pipeline_mocks_p3(
        _run_all_games_with_alerts=MagicMock(
            return_value={"signals": 7, "alerts": 3}
        )
    )
    db = MagicMock()
    with patch.multiple(_PM, **mocks):
        result = run_full_pipeline(db, skip_historical=True)

    assert result.signals_generated == 7
    assert result.alerts_sent == 3

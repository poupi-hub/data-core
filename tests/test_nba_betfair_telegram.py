"""
Tests for NBA Betfair connector and Telegram alert changes.
All tests are unit-level and do NOT hit real APIs.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── Telegram dedup ────────────────────────────────────────────────────────────

def _make_signal(sent=False):
    sig = MagicMock()
    sig.id = "sig-1"
    sig.setup_name = "BACK_TO_BACK_FADE_V1"
    sig.market_type = "moneyline"
    sig.selection = "Boston Celtics"
    sig.line = None
    sig.odd = -110
    sig.signal_direction = "home"
    sig.rationale = "Away on B2B"
    sig.confidence = 0.8
    sig.telegram_sent_at = datetime.now(timezone.utc) if sent else None
    return sig


def _make_game():
    game = MagicMock()
    game.home_team = "Boston Celtics"
    game.away_team = "Miami Heat"
    game.game_date = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)
    game.home_score = None
    game.away_score = None
    return game


class TestTelegramDedup:
    def test_skip_if_already_sent(self):
        """Signal with telegram_sent_at set must not be sent again."""
        from app.modules.nba.quant.telegram_alerts import send_signal_alert
        sig = _make_signal(sent=True)
        with patch("app.modules.nba.quant.telegram_alerts.send_alert") as mock_send:
            result = send_signal_alert(sig, _make_game())
        mock_send.assert_not_called()
        assert result is False

    def test_send_if_not_yet_sent(self):
        """Signal without telegram_sent_at should attempt send."""
        from app.modules.nba.quant.telegram_alerts import send_signal_alert
        sig = _make_signal(sent=False)
        with patch("app.modules.nba.quant.telegram_alerts.send_alert", return_value=True) as mock_send:
            result = send_signal_alert(sig, _make_game())
        mock_send.assert_called_once()
        assert result is True

    def test_marks_sent_at_on_success(self):
        """send_signal_alert must set telegram_sent_at when db is provided."""
        from app.modules.nba.quant.telegram_alerts import send_signal_alert
        sig = _make_signal(sent=False)
        db = MagicMock()
        with patch("app.modules.nba.quant.telegram_alerts.send_alert", return_value=True):
            send_signal_alert(sig, _make_game(), db=db)
        assert sig.telegram_sent_at is not None
        db.add.assert_called_once_with(sig)
        db.commit.assert_called_once()

    def test_does_not_mark_sent_at_on_failure(self):
        """If send_alert fails, telegram_sent_at must remain None."""
        from app.modules.nba.quant.telegram_alerts import send_signal_alert
        sig = _make_signal(sent=False)
        db = MagicMock()
        with patch("app.modules.nba.quant.telegram_alerts.send_alert", return_value=False):
            send_signal_alert(sig, _make_game(), db=db)
        assert sig.telegram_sent_at is None
        db.commit.assert_not_called()


class TestTelegramGuards:
    def test_blocked_when_nba_sim_disabled(self):
        """ENABLE_NBA_TELEGRAM_SIMULATIONS=false must block send."""
        with patch.dict(os.environ, {
            "TELEGRAM_ENABLED": "true",
            "ENABLE_NBA_TELEGRAM_SIMULATIONS": "false",
            "TELEGRAM_BOT_TOKEN": "token",
            "EXECUTIVE_CHAT_ID": "123",
        }):
            # Re-import to pick up new env
            import importlib
            import app.modules.nba.quant.telegram_alerts as mod
            importlib.reload(mod)
            ok, reason = mod._is_configured()
            assert not ok
            assert "ENABLE_NBA_TELEGRAM_SIMULATIONS" in reason

    def test_validate_config_returns_dict(self):
        from app.modules.nba.quant.telegram_alerts import validate_config
        cfg = validate_config()
        assert "configured" in cfg
        assert "nba_simulations_enabled" in cfg
        assert "chat_id_source" in cfg


# ── Betfair config ────────────────────────────────────────────────────────────

class TestBetfairConfig:
    def test_not_configured_when_no_envs(self):
        with patch.dict(os.environ, {"BETFAIR_USERNAME": "", "BETFAIR_PASSWORD": "", "BETFAIR_APP_KEY": ""}):
            import importlib
            import app.modules.nba.quant.betfair_collector as mod
            importlib.reload(mod)
            assert mod.is_configured() is False

    def test_configured_when_all_envs_set(self):
        with patch.dict(os.environ, {
            "BETFAIR_USERNAME": "user",
            "BETFAIR_PASSWORD": "pass",
            "BETFAIR_APP_KEY": "key",
        }):
            import importlib
            import app.modules.nba.quant.betfair_collector as mod
            importlib.reload(mod)
            assert mod.is_configured() is True

    def test_validate_config_never_exposes_secrets(self):
        with patch.dict(os.environ, {
            "BETFAIR_USERNAME": "myuser",
            "BETFAIR_PASSWORD": "secret123",
            "BETFAIR_APP_KEY": "appkey",
        }):
            import importlib
            import app.modules.nba.quant.betfair_collector as mod
            importlib.reload(mod)
            cfg = mod.validate_config()
            dumped = str(cfg)
            assert "secret123" not in dumped
            assert "myuser" not in dumped
            assert cfg["username_set"] is True
            assert cfg["password_set"] is True

    def test_get_client_raises_on_missing_creds(self):
        with patch.dict(os.environ, {"BETFAIR_USERNAME": "", "BETFAIR_PASSWORD": "", "BETFAIR_APP_KEY": ""}):
            import importlib
            import app.modules.nba.quant.betfair_collector as mod
            importlib.reload(mod)
            # Raises ValueError (missing creds) if betfairlightweight is installed,
            # or ImportError if not yet installed locally.
            with pytest.raises((ValueError, ImportError)):
                mod._get_client()


# ── Format functions ──────────────────────────────────────────────────────────

class TestFormatting:
    def test_format_signal_alert_no_crash(self):
        from app.modules.nba.quant.telegram_alerts import format_signal_alert
        sig = _make_signal()
        game = _make_game()
        text = format_signal_alert(sig, game)
        assert "NBA Quant" in text
        assert "BACK_TO_BACK_FADE_V1" in text
        assert "Boston Celtics" in text

    def test_format_settlement_alert_won(self):
        from app.modules.nba.quant.telegram_alerts import format_settlement_alert
        from app.modules.nba.quant.models import BetStatus
        sig = _make_signal()
        bet = MagicMock()
        bet.status = BetStatus.won
        bet.pnl = 0.909
        game = _make_game()
        game.home_score = 110
        game.away_score = 98
        text = format_settlement_alert(sig, bet, game)
        assert "WON" in text
        assert "+0.91" in text

    def test_settlement_alert_skips_pending(self):
        from app.modules.nba.quant.telegram_alerts import send_settlement_alert
        from app.modules.nba.quant.models import BetStatus
        sig = _make_signal()
        bet = MagicMock()
        bet.status = BetStatus.pending
        bet.settlement_telegram_sent_at = None
        with patch("app.modules.nba.quant.telegram_alerts.send_alert") as mock_send:
            result = send_settlement_alert(sig, bet, _make_game())
        mock_send.assert_not_called()
        assert result is False

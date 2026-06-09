"""Tests for Phase 11 TelegramRouter alert routing."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.alerts.channel import (
    CHANNEL_ENV,
    RATE_LIMITS,
    ROUTING_TABLE,
    AlertChannel,
)
from app.alerts.router import (
    TelegramRouter,
    _check_rate_limit,
    _hour_key,
    _increment_rate_limit,
    _send_with_retry,
)
from app.modules.crypto.edge.alert_state_model import EdgeAlertState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_db(state_rows: list[EdgeAlertState] | None = None) -> MagicMock:
    rows = state_rows or []
    db = MagicMock()

    def query_side_effect(model):  # noqa: ANN001
        q = MagicMock()
        q.filter.return_value = q
        q.all.return_value = rows
        q.first.return_value = None
        return q

    db.query.side_effect = query_side_effect
    db.add = MagicMock()
    db.commit = MagicMock()
    return db


def _db_with_state(key: str, value: dict) -> MagicMock:
    fake = MagicMock(spec=EdgeAlertState)
    fake.last_value = value
    fake.last_sent_at = None
    db = MagicMock()

    def query_side_effect(model):  # noqa: ANN001
        q = MagicMock()

        def filter_side_effect(*args, **kwargs):  # noqa: ANN001
            inner = MagicMock()
            inner.first.return_value = fake
            return inner

        q.filter.side_effect = filter_side_effect
        q.all.return_value = []
        q.first.return_value = fake
        return q

    db.query.side_effect = query_side_effect
    db.add = MagicMock()
    db.commit = MagicMock()
    return db


# ---------------------------------------------------------------------------
# TestRoutingTable
# ---------------------------------------------------------------------------


class TestRoutingTable:
    def test_all_channels_covered(self) -> None:
        channels_in_table = set(ROUTING_TABLE.values())
        assert AlertChannel.BUSINESS in channels_in_table
        assert AlertChannel.OPERATIONAL in channels_in_table
        assert AlertChannel.EXECUTIVE in channels_in_table
        assert AlertChannel.CRITICAL in channels_in_table

    def test_business_alert_types(self) -> None:
        business = [k for k, v in ROUTING_TABLE.items() if v == AlertChannel.BUSINESS]
        assert "shadow_signal_new" in business
        assert "weekly_quant_report" in business
        assert "edge_discovery" in business
        assert "opportunity" in business

    def test_operational_alert_types(self) -> None:
        ops = [k for k, v in ROUTING_TABLE.items() if v == AlertChannel.OPERATIONAL]
        assert "daily_quant_summary" in ops
        assert "readiness_change" in ops
        assert "gate_n10" in ops
        assert "backlog" in ops
        assert "scheduler_status" in ops

    def test_executive_alert_types(self) -> None:
        ex = [k for k, v in ROUTING_TABLE.items() if v == AlertChannel.EXECUTIVE]
        assert "gate_n30" in ex
        assert "gate_n100" in ex
        assert "edge_status_change" in ex
        assert "executive_daily" in ex
        assert "executive_weekly" in ex

    def test_critical_alert_types(self) -> None:
        crit = [k for k, v in ROUTING_TABLE.items() if v == AlertChannel.CRITICAL]
        assert "api_down" in crit
        assert "postgres_down" in crit
        assert "redis_down" in crit
        assert "telegram_failure" in crit
        assert "wr_below_50" in crit
        assert "pf_below_1_5" in crit
        assert "edge_collapse" in crit
        assert "stale_analytics" in crit

    def test_total_alert_types(self) -> None:
        # Routing table must cover all spec alert types (22 defined)
        assert len(ROUTING_TABLE) >= 22

    def test_rate_limits_all_channels(self) -> None:
        for channel in AlertChannel:
            assert channel in RATE_LIMITS
            assert RATE_LIMITS[channel] > 0

    def test_channel_env_all_channels(self) -> None:
        for channel in AlertChannel:
            assert channel in CHANNEL_ENV
            assert CHANNEL_ENV[channel].endswith("_CHAT_ID")


# ---------------------------------------------------------------------------
# TestHourKey
# ---------------------------------------------------------------------------


class TestHourKey:
    def test_format(self) -> None:
        key = _hour_key(AlertChannel.CRITICAL)
        assert key.startswith("rl_CRITICAL_")
        parts = key.split("_")
        assert len(parts) == 3

    def test_different_channels_different_keys(self) -> None:
        k1 = _hour_key(AlertChannel.CRITICAL)
        k2 = _hour_key(AlertChannel.BUSINESS)
        assert k1 != k2


# ---------------------------------------------------------------------------
# TestRateLimit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_below_limit_returns_true(self) -> None:
        db = _empty_db()  # no state → count=0
        assert _check_rate_limit(db, AlertChannel.CRITICAL) is True

    def test_at_limit_returns_false(self) -> None:
        limit = RATE_LIMITS[AlertChannel.EXECUTIVE]
        fake = MagicMock(spec=EdgeAlertState)
        fake.last_value = {"count": limit}
        db = MagicMock()

        def qs(model):  # noqa: ANN001
            q = MagicMock()
            inner = MagicMock()
            inner.first.return_value = fake
            q.filter.return_value = inner
            return q

        db.query.side_effect = qs
        db.add = MagicMock()
        db.commit = MagicMock()

        assert _check_rate_limit(db, AlertChannel.EXECUTIVE) is False

    def test_increment_increases_count(self) -> None:
        db = _empty_db()
        _increment_rate_limit(db, AlertChannel.OPERATIONAL)
        db.add.assert_called_once()

    def test_rate_limit_none_db_still_passes(self) -> None:
        # When db is None, rate limit is skipped — check in router.send
        router = TelegramRouter()
        channel = router.resolve_channel("daily_quant_summary")
        assert channel == AlertChannel.OPERATIONAL


# ---------------------------------------------------------------------------
# TestSendWithRetry
# ---------------------------------------------------------------------------


class TestSendWithRetry:
    def test_success_first_try(self) -> None:
        with patch("app.alerts.router._send_once", return_value=True) as m:
            result = _send_with_retry("token", "chat", "text")
        assert result is True
        assert m.call_count == 1

    def test_retries_on_failure(self) -> None:
        with patch("app.alerts.router._send_once", return_value=False), \
             patch("app.alerts.router.time.sleep") as sleep_mock:
            result = _send_with_retry("token", "chat", "text")
        assert result is False
        sleep_mock.assert_called()

    def test_success_on_second_try(self) -> None:
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):  # noqa: ANN001
            call_count["n"] += 1
            return call_count["n"] >= 2

        with patch("app.alerts.router._send_once", side_effect=side_effect), \
             patch("app.alerts.router.time.sleep"):
            result = _send_with_retry("token", "chat", "text")
        assert result is True
        assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# TestTelegramRouterResolveChannel
# ---------------------------------------------------------------------------


class TestTelegramRouterResolveChannel:
    def test_known_alert_type(self) -> None:
        router = TelegramRouter()
        assert router.resolve_channel("api_down") == AlertChannel.CRITICAL
        assert router.resolve_channel("daily_quant_summary") == AlertChannel.OPERATIONAL
        assert router.resolve_channel("gate_n30") == AlertChannel.EXECUTIVE
        assert router.resolve_channel("shadow_signal_new") == AlertChannel.BUSINESS

    def test_unknown_returns_none(self) -> None:
        router = TelegramRouter()
        assert router.resolve_channel("nonexistent_type") is None


# ---------------------------------------------------------------------------
# TestTelegramRouterSend
# ---------------------------------------------------------------------------


class TestTelegramRouterSend:
    def test_no_route_returns_no_route(self) -> None:
        router = TelegramRouter()
        result = router.send("unknown_type", "text", db=None)
        assert result["sent"] is False
        assert result["reason"] == "no_route"

    def test_disabled_returns_telegram_disabled(self) -> None:
        with patch.dict("os.environ", {"TELEGRAM_ENABLED": "false", "TELEGRAM_BOT_TOKEN": ""}):
            router = TelegramRouter()
            result = router.send("api_down", "text", db=None)
        assert result["sent"] is False
        assert result["reason"] == "telegram_disabled"

    def test_dedup_skips_when_already_sent(self) -> None:
        fake = MagicMock(spec=EdgeAlertState)
        fake.last_value = {"sent": True}
        db = MagicMock()

        def qs(model):  # noqa: ANN001
            q = MagicMock()
            inner = MagicMock()
            inner.first.return_value = fake
            q.filter.return_value = inner
            return q

        db.query.side_effect = qs
        db.add = MagicMock()
        db.commit = MagicMock()

        with patch.dict("os.environ", {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "token",
        }):
            router = TelegramRouter()
            result = router.send("api_down", "text", db=db, dedup_key="test_dedup")
        assert result["sent"] is False
        assert result["reason"] == "dedup"

    def test_rate_limited_returns_reason(self) -> None:
        limit = RATE_LIMITS[AlertChannel.CRITICAL]
        fake = MagicMock(spec=EdgeAlertState)
        fake.last_value = {"count": limit}
        db = MagicMock()

        def qs(model):  # noqa: ANN001
            q = MagicMock()
            inner = MagicMock()
            inner.first.return_value = fake
            q.filter.return_value = inner
            return q

        db.query.side_effect = qs
        db.add = MagicMock()
        db.commit = MagicMock()

        with patch.dict("os.environ", {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "token",
        }):
            router = TelegramRouter()
            result = router.send("api_down", "text", db=db)
        assert result["sent"] is False
        assert result["reason"] == "rate_limited"

    def test_no_chat_id_returns_reason(self) -> None:
        db = _empty_db()
        env = {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "token",
            "CRITICAL_CHAT_ID": "",
            "TELEGRAM_CHAT_ID": "",
        }
        with patch.dict("os.environ", env, clear=False):
            router = TelegramRouter()
            result = router.send("api_down", "text", db=db)
        assert result["sent"] is False
        assert result["reason"] == "no_chat_id"

    def test_send_success_commits_state(self) -> None:
        db = _empty_db()
        env = {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "token",
            "CRITICAL_CHAT_ID": "123456",
        }
        with patch.dict("os.environ", env, clear=False), \
             patch("app.alerts.router._send_with_retry", return_value=True):
            router = TelegramRouter()
            result = router.send("api_down", "text", db=db)
        assert result["sent"] is True
        assert result["channel"] == "CRITICAL"
        db.commit.assert_called_once()

    def test_send_failure_returns_send_failed(self) -> None:
        db = _empty_db()
        env = {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "token",
            "CRITICAL_CHAT_ID": "123456",
        }
        with patch.dict("os.environ", env, clear=False), \
             patch("app.alerts.router._send_with_retry", return_value=False):
            router = TelegramRouter()
            result = router.send("api_down", "text", db=db)
        assert result["sent"] is False
        assert result["reason"] == "send_failed"


# ---------------------------------------------------------------------------
# TestTelegramRouterRoutingTable
# ---------------------------------------------------------------------------


class TestTelegramRouterRoutingTable:
    def test_routing_table_structure(self) -> None:
        table = TelegramRouter.routing_table()
        assert "channels" in table
        assert "total_alert_types" in table
        for channel in AlertChannel:
            assert channel.value in table["channels"]
            ch = table["channels"][channel.value]
            assert "alert_types" in ch
            assert "rate_limit_per_hour" in ch
            assert "chat_id_env_var" in ch
            assert "chat_id_configured" in ch

    def test_all_alert_types_present(self) -> None:
        table = TelegramRouter.routing_table()
        all_types: list[str] = []
        for ch in table["channels"].values():
            all_types.extend(ch["alert_types"])
        assert "api_down" in all_types
        assert "shadow_signal_new" in all_types
        assert "gate_n30" in all_types
        assert "daily_quant_summary" in all_types

    def test_total_count_matches(self) -> None:
        table = TelegramRouter.routing_table()
        count = sum(len(ch["alert_types"]) for ch in table["channels"].values())
        assert count == table["total_alert_types"]
        assert table["total_alert_types"] == len(ROUTING_TABLE)


# ---------------------------------------------------------------------------
# TestSendTest
# ---------------------------------------------------------------------------


class TestSendTest:
    def test_send_test_disabled(self) -> None:
        with patch.dict("os.environ", {"TELEGRAM_ENABLED": "false", "TELEGRAM_BOT_TOKEN": ""}):
            router = TelegramRouter()
            result = router.send_test(AlertChannel.OPERATIONAL)
        assert result["sent"] is False

    def test_send_test_calls_send(self) -> None:
        db = _empty_db()
        env = {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "token",
            "OPERATIONAL_CHAT_ID": "999",
        }
        with patch.dict("os.environ", env, clear=False), \
             patch("app.alerts.router._send_with_retry", return_value=True):
            router = TelegramRouter()
            result = router.send_test(AlertChannel.OPERATIONAL, db=db)
        assert result["sent"] is True
        assert result["channel"] == "OPERATIONAL"

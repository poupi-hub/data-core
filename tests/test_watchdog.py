"""Tests for the Operational Watchdog subsystem.

Covers:
  - WatchdogAlert + CheckResult dataclasses
  - CollectionHealthChecker logic
  - NormalizationHealthChecker logic
  - ScraperQualityChecker logic
  - TelegramPublicationChecker logic
  - TelegramNotifier (disabled / success / HTTP failure paths)
  - HeartbeatFormatter output
  - WatchdogService orchestration

All tests use mocked SQLAlchemy sessions and external HTTP — no real DB needed.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.watchdog.checks import CheckResult, WatchdogAlert

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _hours_ago(h: float) -> datetime:
    return _now() - timedelta(hours=h)


def _make_alert(
    severity: str = "warning",
    code: str = "test_code",
    title: str = "Test",
    message: str = "Test message",
    source_name: str | None = None,
) -> WatchdogAlert:
    return WatchdogAlert(
        severity=severity,
        code=code,
        title=title,
        message=message,
        source_name=source_name,
    )


def _make_result(
    name: str = "collection",
    status: str = "ok",
    alerts: list[WatchdogAlert] | None = None,
    metrics: dict | None = None,
) -> CheckResult:
    return CheckResult(
        name=name,
        status=status,
        summary="Test summary",
        alerts=alerts or [],
        metrics=metrics or {},
    )


# ─── WatchdogAlert + CheckResult dataclasses ─────────────────────────────────

class TestWatchdogAlert:
    def test_minimal_construction(self):
        alert = WatchdogAlert(severity="critical", code="test", title="T", message="M")
        assert alert.severity == "critical"
        assert alert.code == "test"
        assert alert.source_name is None
        assert alert.context == {}

    def test_with_context(self):
        alert = WatchdogAlert(
            severity="warning",
            code="code",
            title="Title",
            message="Msg",
            source_name="drogaraia",
            context={"hours": 3},
        )
        assert alert.source_name == "drogaraia"
        assert alert.context == {"hours": 3}


class TestCheckResult:
    def test_defaults(self):
        result = CheckResult(name="collection", status="ok", summary="All good")
        assert result.alerts == []
        assert result.metrics == {}

    def test_to_dict_structure(self):
        alert = _make_alert(severity="critical", code="c_stale", source_name="drogasil")
        result = CheckResult(
            name="collection",
            status="critical",
            summary="Stale",
            alerts=[alert],
            metrics={"active": 0},
        )
        d = result.to_dict()
        assert d["name"] == "collection"
        assert d["status"] == "critical"
        assert len(d["alerts"]) == 1
        a = d["alerts"][0]
        assert a["severity"] == "critical"
        assert a["code"] == "c_stale"
        assert a["source_name"] == "drogasil"
        assert d["metrics"] == {"active": 0}

    def test_to_dict_no_alerts(self):
        result = _make_result()
        d = result.to_dict()
        assert d["alerts"] == []


# ─── HeartbeatFormatter ───────────────────────────────────────────────────────

class TestHeartbeatFormatter:
    def setup_method(self):
        from app.watchdog.heartbeat import HeartbeatFormatter
        self.fmt = HeartbeatFormatter()

    def _check_results(self, statuses: dict[str, str]) -> list[CheckResult]:
        results = []
        for name, status in statuses.items():
            metrics: dict = {}
            if name == "collection":
                metrics = {
                    "active_sources_last_window": 3,
                    "last_raw_collection_age_seconds": 2700,
                    "stale_sources": [],
                }
            elif name == "normalization":
                metrics = {
                    "source_rates": {"drogaraia": {"normalized": 10, "success_rate": 0.95}},
                    "normalization_pending_total": 2,
                    "last_normalized_age_seconds": 3600,
                }
            elif name == "scraper_quality":
                metrics = {
                    "quality_stats": {},
                    "anti_bot_by_source_1h": {},
                    "open_drift_events": [],
                }
            elif name == "telegram":
                metrics = {
                    "telegram_sent_24h": 5,
                    "telegram_failed_24h": 0,
                    "last_telegram_post_age_seconds": 7200,
                    "status_note": "",
                }
            results.append(_make_result(name=name, status=status, metrics=metrics))
        return results

    def test_ok_status_uses_healthy_title(self):
        results = self._check_results({
            "collection": "ok",
            "normalization": "ok",
            "scraper_quality": "ok",
            "telegram": "ok",
        })
        msg = self.fmt.format(results, heartbeat_interval_hours=6)
        assert "Poupi saudável" in msg
        assert "✅" in msg

    def test_critical_status_shown(self):
        results = self._check_results({
            "collection": "critical",
            "normalization": "ok",
            "scraper_quality": "ok",
            "telegram": "ok",
        })
        msg = self.fmt.format(results)
        assert "🔴" in msg
        assert "CRÍTICO" in msg

    def test_warning_status_shown(self):
        results = self._check_results({
            "collection": "warning",
            "normalization": "ok",
            "scraper_quality": "ok",
            "telegram": "ok",
        })
        msg = self.fmt.format(results)
        assert "⚠️" in msg
        assert "ATENÇÃO" in msg

    def test_next_heartbeat_footer(self):
        results = self._check_results({"collection": "ok"})
        msg = self.fmt.format(results, heartbeat_interval_hours=12)
        assert "em ~12h" in msg

    def test_alerts_summary_included(self):
        alert = _make_alert(severity="critical", title="Coleta parada")
        results = [_make_result(
            name="collection", status="critical", alerts=[alert],
            metrics={
                "active_sources_last_window": 0,
                "last_raw_collection_age_seconds": None,
                "stale_sources": ["drogasil"],
            },
        )]
        msg = self.fmt.format(results)
        assert "Alertas ativos" in msg
        assert "Coleta parada" in msg

    def test_telegram_no_callback_data(self):
        results = [_make_result(
            name="telegram", status="ok",
            metrics={"status_note": "no_callback_data"},
        )]
        msg = self.fmt.format(results)
        assert "Callback não configurado" in msg

    def test_too_many_alerts_truncated(self):
        alerts = [_make_alert(title=f"Alert {i}") for i in range(8)]
        results = [_make_result(name="collection", status="warning", alerts=alerts)]
        msg = self.fmt.format(results)
        assert "+3 outros" in msg

    def test_format_alert_message(self):
        from app.watchdog.heartbeat import format_alert_message
        msg = format_alert_message(
            alert_code="test_code",
            title="Alerta Teste",
            message="Algo aconteceu",
            severity="critical",
        )
        assert "🔴" in msg
        assert "<b>Alerta Teste</b>" in msg
        assert "Algo aconteceu" in msg
        assert "test_code" in msg

    def test_format_warning_alert_message(self):
        from app.watchdog.heartbeat import format_alert_message
        msg = format_alert_message("w_code", "Aviso", "Cuidado", severity="warning")
        assert "⚠️" in msg


# ─── TelegramNotifier ────────────────────────────────────────────────────────

class TestTelegramNotifier:
    def _make_notifier(self, token: str = "tok", chat_id: str = "123") -> object:
        from app.watchdog.notifier import TelegramNotifier
        with patch("app.watchdog.notifier.settings") as mock_settings:
            mock_settings.telegram_enabled = True
            mock_settings.telegram_bot_token = token
            mock_settings.telegram_chat_id = chat_id
            notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
            notifier._enabled = True  # force-enable for tests
        return notifier

    def test_disabled_by_default_returns_false(self):
        from app.watchdog.notifier import TelegramNotifier
        with patch("app.watchdog.notifier.settings") as mock_settings:
            mock_settings.telegram_enabled = False
            mock_settings.telegram_bot_token = ""
            mock_settings.telegram_chat_id = ""
            notifier = TelegramNotifier()
        assert notifier.is_enabled is False
        assert notifier.send("hello") is False

    def test_send_html_success(self):
        notifier = self._make_notifier()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("app.watchdog.notifier.httpx.post", return_value=mock_resp) as mock_post:
            result = notifier.send("<b>Test</b>")
        assert result is True
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"]["parse_mode"] == "HTML"
        assert call_kwargs[1]["json"]["text"] == "<b>Test</b>"

    def test_send_plain_omits_parse_mode(self):
        notifier = self._make_notifier()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("app.watchdog.notifier.httpx.post", return_value=mock_resp) as mock_post:
            result = notifier.send_plain("plain text")
        assert result is True
        call_kwargs = mock_post.call_args
        assert "parse_mode" not in call_kwargs[1]["json"]

    def test_http_non_200_returns_false(self):
        notifier = self._make_notifier()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        with patch("app.watchdog.notifier.httpx.post", return_value=mock_resp):
            result = notifier.send("test")
        assert result is False

    def test_timeout_returns_false(self):
        import httpx
        notifier = self._make_notifier()
        with patch("app.watchdog.notifier.httpx.post", side_effect=httpx.TimeoutException("timeout")):
            result = notifier.send("test")
        assert result is False

    def test_exception_returns_false(self):
        notifier = self._make_notifier()
        with patch("app.watchdog.notifier.httpx.post", side_effect=RuntimeError("boom")):
            result = notifier.send("test")
        assert result is False


# ─── _worst_status helper (shared across checks) ─────────────────────────────

class TestWorstStatus:
    """The _worst_status helper is duplicated across check modules — test one copy."""

    def _worst(self, alerts):
        from app.watchdog.checks.collection import _worst_status
        return _worst_status(alerts)

    def test_no_alerts_returns_ok(self):
        assert self._worst([]) == "ok"

    def test_warning_only(self):
        assert self._worst([_make_alert("warning")]) == "warning"

    def test_critical_dominates(self):
        alerts = [_make_alert("warning"), _make_alert("critical")]
        assert self._worst(alerts) == "critical"

    def test_all_critical(self):
        alerts = [_make_alert("critical"), _make_alert("critical")]
        assert self._worst(alerts) == "critical"


# ─── CollectionHealthChecker ──────────────────────────────────────────────────

class TestCollectionHealthChecker:
    """Unit tests using a fully mocked SQLAlchemy session."""

    def _make_db(self) -> MagicMock:
        return MagicMock()

    def _make_row(self, source_name, total, last_at, error_count=0):
        row = MagicMock()
        row.source_name = source_name
        row.total = total
        row.last_collected_at = last_at
        row.error_count = error_count
        return row

    def _checker(self, db):
        from app.watchdog.checks.collection import CollectionHealthChecker
        return CollectionHealthChecker(db)

    def test_ok_when_all_sources_active(self):
        db = self._make_db()
        now = _now()

        # Query 1 (stale window rows): 2 sources active
        rows = [
            self._make_row("drogasil", 10, now - timedelta(minutes=30), 0),
            self._make_row("drogaraia", 8, now - timedelta(minutes=45), 0),
        ]
        # Query 2 (known sources distinct): same 2 sources
        known_rows = [MagicMock(source_name="drogasil"), MagicMock(source_name="drogaraia")]
        # Query 3 (1h window rows): same sources, no errors
        rows_1h = [
            self._make_row("drogasil", 5, now - timedelta(minutes=15), 0),
            self._make_row("drogaraia", 4, now - timedelta(minutes=20), 0),
        ]
        # Query 4 (active target count): 2
        # Query 5 (latest overall): recent
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.distinct.return_value = query_mock

        # Sequence of .all() calls
        query_mock.all.side_effect = [rows, known_rows, rows_1h]
        # Sequence of .scalar() calls
        query_mock.scalar.side_effect = [2, now - timedelta(minutes=30)]

        with patch("app.watchdog.checks.collection.settings") as ms:
            ms.watchdog_collection_stale_hours = 3
            result = self._checker(db).run()

        assert result.status == "ok"
        assert result.name == "collection"
        assert result.alerts == []

    def test_stale_source_creates_critical_alert(self):
        db = self._make_db()
        now = _now()

        # Active window: only drogasil present
        rows = [self._make_row("drogasil", 10, now - timedelta(minutes=20), 0)]
        # Known sources: both
        known_rows = [
            MagicMock(source_name="drogasil"),
            MagicMock(source_name="drogaraia"),
        ]
        rows_1h = [self._make_row("drogasil", 5, now - timedelta(minutes=10), 0)]

        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.distinct.return_value = query_mock

        stale_last = now - timedelta(hours=5)
        query_mock.all.side_effect = [rows, known_rows, rows_1h]
        # scalar() order: stale src last_at (per stale src), active_target_count, latest_overall
        query_mock.scalar.side_effect = [stale_last, 2, now - timedelta(minutes=20)]

        with patch("app.watchdog.checks.collection.settings") as ms:
            ms.watchdog_collection_stale_hours = 3
            result = self._checker(db).run()

        assert result.status == "critical"
        codes = [a.code for a in result.alerts]
        assert "collection_stale" in codes
        stale_alert = next(a for a in result.alerts if a.code == "collection_stale")
        assert stale_alert.source_name == "drogaraia"

    def test_platform_down_when_no_active_sources(self):
        db = self._make_db()
        now = _now()

        rows = []  # no active sources
        known_rows = [MagicMock(source_name="drogasil"), MagicMock(source_name="drogaraia")]
        rows_1h = []

        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.distinct.return_value = query_mock

        # scalar() calls: count(active_targets), last_known_at drogaraia, last_known_at drogasil, active_targets, latest_overall
        stale_ts = now - timedelta(hours=6)
        query_mock.all.side_effect = [rows, known_rows, rows_1h]
        query_mock.scalar.side_effect = [stale_ts, stale_ts, 2, None]

        with patch("app.watchdog.checks.collection.settings") as ms:
            ms.watchdog_collection_stale_hours = 3
            result = self._checker(db).run()

        assert result.status == "critical"
        codes = [a.code for a in result.alerts]
        assert "collection_platform_down" in codes

    def test_high_failure_rate_creates_warning(self):
        db = self._make_db()
        now = _now()

        rows = [self._make_row("drogasil", 10, now - timedelta(minutes=20), 0)]
        known_rows = [MagicMock(source_name="drogasil")]
        # 1h window: 10 total, 5 errors = 50% → above 40% threshold
        rows_1h_mock = MagicMock()
        rows_1h_mock.source_name = "drogasil"
        rows_1h_mock.total = 10
        rows_1h_mock.error_count = 5

        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.distinct.return_value = query_mock

        query_mock.all.side_effect = [rows, known_rows, [rows_1h_mock]]
        query_mock.scalar.side_effect = [2, now - timedelta(minutes=20)]

        with patch("app.watchdog.checks.collection.settings") as ms:
            ms.watchdog_collection_stale_hours = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "collection_high_failure_rate" in codes
        assert result.status in ("warning", "critical")

    def test_low_failure_rate_not_flagged(self):
        db = self._make_db()
        now = _now()

        rows = [self._make_row("drogasil", 10, now - timedelta(minutes=20), 1)]
        known_rows = [MagicMock(source_name="drogasil")]
        # 1h window: 10 total, 2 errors = 20% → below threshold
        rows_1h_mock = MagicMock()
        rows_1h_mock.source_name = "drogasil"
        rows_1h_mock.total = 10
        rows_1h_mock.error_count = 2

        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.distinct.return_value = query_mock

        query_mock.all.side_effect = [rows, known_rows, [rows_1h_mock]]
        query_mock.scalar.side_effect = [2, now - timedelta(minutes=20)]

        with patch("app.watchdog.checks.collection.settings") as ms:
            ms.watchdog_collection_stale_hours = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "collection_high_failure_rate" not in codes

    def test_small_1h_sample_not_flagged(self):
        """Fewer than 3 samples in 1h window should not trigger failure rate alert."""
        db = self._make_db()
        now = _now()

        rows = [self._make_row("drogasil", 2, now - timedelta(minutes=20), 2)]
        known_rows = [MagicMock(source_name="drogasil")]
        rows_1h_mock = MagicMock()
        rows_1h_mock.source_name = "drogasil"
        rows_1h_mock.total = 2  # < 3
        rows_1h_mock.error_count = 2

        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.distinct.return_value = query_mock

        query_mock.all.side_effect = [rows, known_rows, [rows_1h_mock]]
        query_mock.scalar.side_effect = [2, now - timedelta(minutes=20)]

        with patch("app.watchdog.checks.collection.settings") as ms:
            ms.watchdog_collection_stale_hours = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "collection_high_failure_rate" not in codes

    def test_exception_returns_warning(self):
        db = self._make_db()
        db.query.side_effect = RuntimeError("DB down")
        from app.watchdog.checks.collection import CollectionHealthChecker
        result = CollectionHealthChecker(db).run()
        assert result.status == "warning"
        assert "Collection check error" in result.summary


# ─── NormalizationHealthChecker ───────────────────────────────────────────────

class TestNormalizationHealthChecker:
    def _checker(self, db):
        from app.watchdog.checks.normalization import NormalizationHealthChecker
        return NormalizationHealthChecker(db)

    def _make_db_with_no_backlog(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.all.return_value = []
        # pending_old=0, pending_total=0, last_normalized_at=recent
        query_mock.scalar.side_effect = [0, 0, _now() - timedelta(hours=1)]
        return db

    def test_ok_when_no_backlog(self):
        db = self._make_db_with_no_backlog()
        with patch("app.watchdog.checks.normalization.settings") as ms:
            ms.watchdog_normalization_backlog_minutes = 45
            result = self._checker(db).run()
        assert result.status == "ok"
        assert result.alerts == []

    def test_backlog_warning_when_old_pending(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.all.return_value = []
        # pending_old=5, pending_total=10, last_normalized_at=recent
        query_mock.scalar.side_effect = [5, 10, _now() - timedelta(hours=1)]

        with patch("app.watchdog.checks.normalization.settings") as ms:
            ms.watchdog_normalization_backlog_minutes = 45
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "normalization_backlog" in codes
        backlog_alert = next(a for a in result.alerts if a.code == "normalization_backlog")
        assert backlog_alert.severity == "warning"

    def test_large_backlog_is_critical(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.all.return_value = []
        query_mock.scalar.side_effect = [25, 30, _now() - timedelta(hours=1)]

        with patch("app.watchdog.checks.normalization.settings") as ms:
            ms.watchdog_normalization_backlog_minutes = 45
            result = self._checker(db).run()

        backlog_alert = next(a for a in result.alerts if a.code == "normalization_backlog")
        assert backlog_alert.severity == "critical"

    def test_low_success_rate_creates_warning(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        # status_rows: source_name, processing_status, cnt
        status_rows = [
            MagicMock(source_name="drogasil", processing_status="normalized", cnt=3),
            MagicMock(source_name="drogasil", processing_status="normalization_failed", cnt=10),
        ]
        query_mock.all.return_value = status_rows
        query_mock.scalar.side_effect = [0, 0, _now() - timedelta(hours=1)]

        with patch("app.watchdog.checks.normalization.settings") as ms:
            ms.watchdog_normalization_backlog_minutes = 45
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "normalization_low_success_rate" in codes

    def test_old_last_normalized_creates_warning(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.all.return_value = []
        # last normalized was 6h ago → > 4h threshold
        query_mock.scalar.side_effect = [0, 0, _now() - timedelta(hours=6)]

        with patch("app.watchdog.checks.normalization.settings") as ms:
            ms.watchdog_normalization_backlog_minutes = 45
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "normalization_stale" in codes

    def test_exception_returns_warning(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("DB down")
        from app.watchdog.checks.normalization import NormalizationHealthChecker
        result = NormalizationHealthChecker(db).run()
        assert result.status == "warning"
        assert "Normalization check error" in result.summary


# ─── ScraperQualityChecker ────────────────────────────────────────────────────

class TestScraperQualityChecker:
    def _checker(self, db):
        from app.watchdog.checks.scraper_quality import ScraperQualityChecker
        return ScraperQualityChecker(db)

    def _make_empty_db(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.all.return_value = []
        return db

    def test_ok_when_no_data(self):
        db = self._make_empty_db()
        with patch("app.watchdog.checks.scraper_quality.settings") as ms:
            ms.watchdog_quality_score_threshold = 50
            ms.watchdog_anti_bot_hourly_threshold = 3
            result = self._checker(db).run()
        assert result.status == "ok"
        assert result.alerts == []

    def test_low_quality_score_creates_warning(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        # quality_rows: source_name + count
        quality_row = MagicMock()
        quality_row.source_name = "drogasil"
        quality_row.total = 5

        # individual metadata rows with low quality
        meta_rows = [
            MagicMock(metadata_json={"quality": {"score": 30}}),
            MagicMock(metadata_json={"quality": {"score": 25}}),
            MagicMock(metadata_json={"quality": {"score": 40}}),
        ]

        # Return quality_rows on first all(), then meta_rows, then [] for 1h anti-bot, then [] for drift
        query_mock.all.side_effect = [[quality_row], meta_rows, [], []]

        with patch("app.watchdog.checks.scraper_quality.settings") as ms:
            ms.watchdog_quality_score_threshold = 50
            ms.watchdog_anti_bot_hourly_threshold = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "scraper_quality_low" in codes

    def test_high_quality_score_no_alert(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        quality_row = MagicMock()
        quality_row.source_name = "drogasil"
        quality_row.total = 3
        meta_rows = [
            MagicMock(metadata_json={"quality": {"score": 90}}),
            MagicMock(metadata_json={"quality": {"score": 85}}),
        ]
        query_mock.all.side_effect = [[quality_row], meta_rows, [], []]

        with patch("app.watchdog.checks.scraper_quality.settings") as ms:
            ms.watchdog_quality_score_threshold = 50
            ms.watchdog_anti_bot_hourly_threshold = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "scraper_quality_low" not in codes

    def test_anti_bot_spike_creates_warning(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        # No quality rows (first all()), no meta rows
        # 1h window: 4 rows with anti_bot_detected=True
        ab_rows = [
            MagicMock(source_name="drogasil", metadata_json={"anti_bot_detected": True}),
            MagicMock(source_name="drogasil", metadata_json={"anti_bot_detected": True}),
            MagicMock(source_name="drogasil", metadata_json={"anti_bot_detected": True}),
            MagicMock(source_name="drogasil", metadata_json={"anti_bot_detected": True}),
        ]
        query_mock.all.side_effect = [[], ab_rows, []]

        with patch("app.watchdog.checks.scraper_quality.settings") as ms:
            ms.watchdog_quality_score_threshold = 50
            ms.watchdog_anti_bot_hourly_threshold = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "anti_bot_spike" in codes

    def test_anti_bot_below_threshold_no_alert(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        ab_rows = [
            MagicMock(source_name="drogasil", metadata_json={"anti_bot_detected": True}),
            MagicMock(source_name="drogasil", metadata_json={"anti_bot_detected": True}),
        ]  # 2 < threshold of 3
        query_mock.all.side_effect = [[], ab_rows, []]

        with patch("app.watchdog.checks.scraper_quality.settings") as ms:
            ms.watchdog_quality_score_threshold = 50
            ms.watchdog_anti_bot_hourly_threshold = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "anti_bot_spike" not in codes

    def test_open_drift_event_creates_alert(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        drift_event = MagicMock()
        drift_event.source_name = "paguemenos"
        drift_event.drift_type = "price_zero"
        drift_event.risk_level = "high"
        drift_event.resolved_at = None

        query_mock.all.side_effect = [[], [], [drift_event]]

        with patch("app.watchdog.checks.scraper_quality.settings") as ms:
            ms.watchdog_quality_score_threshold = 50
            ms.watchdog_anti_bot_hourly_threshold = 3
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "scraper_drift_detected" in codes

    def test_exception_returns_warning(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("boom")
        from app.watchdog.checks.scraper_quality import ScraperQualityChecker
        result = ScraperQualityChecker(db).run()
        assert result.status == "warning"


# ─── TelegramPublicationChecker ───────────────────────────────────────────────

class TestTelegramPublicationChecker:
    def _checker(self, db):
        from app.watchdog.checks.telegram_pub import TelegramPublicationChecker
        return TelegramPublicationChecker(db)

    def test_no_events_returns_ok_with_note(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock
        query_mock.scalar.return_value = 0  # total_events = 0

        with patch("app.watchdog.checks.telegram_pub.settings") as ms:
            ms.watchdog_publication_stale_hours = 6
            result = self._checker(db).run()

        assert result.status == "ok"
        assert result.alerts == []
        assert "POST /api/v1/watchdog/report/telegram-published" in result.summary

    def test_recent_publication_returns_ok(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        now = _now()
        recent_sent_at = now - timedelta(hours=1)

        # scalar() sequence: total_events=5, last_sent_at=1h_ago, recent_sent=3
        # then by_status_24h via all()
        query_mock.scalar.side_effect = [5, recent_sent_at, 3]
        by_status_row = MagicMock()
        by_status_row.status = "sent"
        by_status_row.cnt = 3
        query_mock.all.return_value = [by_status_row]

        with patch("app.watchdog.checks.telegram_pub.settings") as ms:
            ms.watchdog_publication_stale_hours = 6
            result = self._checker(db).run()

        assert result.status == "ok"
        assert result.alerts == []

    def test_no_recent_publication_with_failures_is_critical(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        now = _now()
        old_sent_at = now - timedelta(hours=8)

        # total_events=5, last_sent_at=8h_ago, recent_sent=0, recent_normalized=0, recent_failed=3
        query_mock.scalar.side_effect = [5, old_sent_at, 0, 0, 3]
        by_status_row = MagicMock()
        by_status_row.status = "failed"
        by_status_row.cnt = 3
        query_mock.all.return_value = [by_status_row]

        with patch("app.watchdog.checks.telegram_pub.settings") as ms:
            ms.watchdog_publication_stale_hours = 6
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "telegram_publish_failing" in codes
        assert result.status == "critical"

    def test_no_publication_with_products_is_warning(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        now = _now()
        old_sent_at = now - timedelta(hours=8)

        # total=5, last_sent=8h_ago, recent_sent=0, recent_normalized=20, recent_failed=0
        query_mock.scalar.side_effect = [5, old_sent_at, 0, 20, 0]
        query_mock.all.return_value = []  # no by_status data

        with patch("app.watchdog.checks.telegram_pub.settings") as ms:
            ms.watchdog_publication_stale_hours = 6
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "telegram_no_publication_products_exist" in codes
        assert result.status == "warning"

    def test_no_publication_no_products_is_warning(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        now = _now()
        old_sent_at = now - timedelta(hours=8)

        # total=5, last_sent=8h_ago, recent_sent=0, recent_normalized=0, recent_failed=0
        query_mock.scalar.side_effect = [5, old_sent_at, 0, 0, 0]
        query_mock.all.return_value = []

        with patch("app.watchdog.checks.telegram_pub.settings") as ms:
            ms.watchdog_publication_stale_hours = 6
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "telegram_no_publication_no_data" in codes

    def test_high_failure_rate_creates_warning(self):
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock
        query_mock.filter.return_value = query_mock
        query_mock.group_by.return_value = query_mock

        now = _now()
        recent_sent_at = now - timedelta(hours=1)

        # total=5, last_sent=recent, recent_sent=1
        query_mock.scalar.side_effect = [5, recent_sent_at, 1]
        # 24h stats: 2 sent, 5 failed → 71% failure rate > 30%
        rows = [
            MagicMock(status="sent", cnt=2),
            MagicMock(status="failed", cnt=5),
        ]
        query_mock.all.return_value = rows

        with patch("app.watchdog.checks.telegram_pub.settings") as ms:
            ms.watchdog_publication_stale_hours = 6
            result = self._checker(db).run()

        codes = [a.code for a in result.alerts]
        assert "telegram_high_failure_rate" in codes

    def test_exception_returns_warning(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("DB crash")
        from app.watchdog.checks.telegram_pub import TelegramPublicationChecker
        result = TelegramPublicationChecker(db).run()
        assert result.status == "warning"
        assert "Telegram check error" in result.summary


# ─── WatchdogService ──────────────────────────────────────────────────────────

class TestWatchdogService:
    """Smoke-tests for WatchdogService orchestration with mocked checkers."""

    def _make_service(self, check_results: list[CheckResult]) -> object:
        from app.watchdog.service import WatchdogService
        db = MagicMock()
        svc = WatchdogService(db)
        svc._run_all_checks = MagicMock(return_value=check_results)
        svc._notifier = MagicMock()
        svc._notifier.send.return_value = False
        svc._notifier.is_enabled = False
        return svc

    def test_run_returns_watchdog_run(self):
        from app.watchdog.models import WatchdogRun
        results = [
            _make_result("collection", "ok"),
            _make_result("normalization", "ok"),
            _make_result("scraper_quality", "ok"),
            _make_result("telegram", "ok"),
        ]
        svc = self._make_service(results)
        # Mock DB add/commit/refresh
        svc._db.add = MagicMock()
        svc._db.commit = MagicMock()
        svc._db.refresh = MagicMock()
        # Prevent Prometheus from breaking
        with patch("app.watchdog.service.operational_watchdog_status"), \
             patch("app.watchdog.service.last_raw_collection_age_seconds"), \
             patch("app.watchdog.service.last_normalized_offer_age_seconds"), \
             patch("app.watchdog.service.last_telegram_post_age_seconds"), \
             patch("app.watchdog.service.raw_to_normalized_success_rate"), \
             patch("app.watchdog.service.domains_with_active_alerts"), \
             patch("app.watchdog.service.telegram_publish_success_total"), \
             patch("app.watchdog.service.telegram_publish_failure_total"), \
             patch("app.watchdog.service.watchdog_checks_total"):
            run = svc.run()

        assert isinstance(run, WatchdogRun)
        assert run.overall_status == "ok"
        assert run.duration_ms >= 0

    def test_critical_alerts_trigger_telegram(self):
        results = [
            _make_result(
                "collection", "critical",
                alerts=[_make_alert("critical", "collection_platform_down", "Plataforma down", "msg")]
            ),
            _make_result("normalization", "ok"),
            _make_result("scraper_quality", "ok"),
            _make_result("telegram", "ok"),
        ]
        svc = self._make_service(results)
        svc._db.add = MagicMock()
        svc._db.commit = MagicMock()
        svc._db.refresh = MagicMock()

        with patch("app.watchdog.service.operational_watchdog_status"), \
             patch("app.watchdog.service.last_raw_collection_age_seconds"), \
             patch("app.watchdog.service.last_normalized_offer_age_seconds"), \
             patch("app.watchdog.service.last_telegram_post_age_seconds"), \
             patch("app.watchdog.service.raw_to_normalized_success_rate"), \
             patch("app.watchdog.service.domains_with_active_alerts"), \
             patch("app.watchdog.service.telegram_publish_success_total"), \
             patch("app.watchdog.service.telegram_publish_failure_total"), \
             patch("app.watchdog.service.watchdog_checks_total"):
            run = svc.run()

        assert run.overall_status == "critical"
        # Notifier.send should have been called for the critical alert
        svc._notifier.send.assert_called()

    def test_heartbeat_calls_send(self):
        results = [_make_result("collection", "ok")]
        svc = self._make_service(results)

        with patch("app.watchdog.service.settings") as ms:
            ms.watchdog_heartbeat_hours = 6
            svc.heartbeat()

        svc._notifier.send.assert_called_once()

    def test_run_persists_to_db(self):
        results = [_make_result("collection", "ok")]
        svc = self._make_service(results)
        svc._db.add = MagicMock()
        svc._db.commit = MagicMock()
        svc._db.refresh = MagicMock()

        with patch("app.watchdog.service.operational_watchdog_status"), \
             patch("app.watchdog.service.last_raw_collection_age_seconds"), \
             patch("app.watchdog.service.last_normalized_offer_age_seconds"), \
             patch("app.watchdog.service.last_telegram_post_age_seconds"), \
             patch("app.watchdog.service.raw_to_normalized_success_rate"), \
             patch("app.watchdog.service.domains_with_active_alerts"), \
             patch("app.watchdog.service.telegram_publish_success_total"), \
             patch("app.watchdog.service.telegram_publish_failure_total"), \
             patch("app.watchdog.service.watchdog_checks_total"):
            svc.run()

        svc._db.add.assert_called_once()
        svc._db.commit.assert_called_once()

    def test_overall_status_critical_when_any_critical(self):
        results = [
            _make_result("collection", "ok"),
            _make_result("normalization", "critical"),
        ]
        svc = self._make_service(results)
        assert svc._overall_status(results) == "critical"

    def test_overall_status_warning_when_no_critical(self):
        results = [
            _make_result("collection", "ok"),
            _make_result("normalization", "warning"),
        ]
        svc = self._make_service(results)
        assert svc._overall_status(results) == "warning"

    def test_overall_status_ok_when_all_ok(self):
        results = [_make_result("collection", "ok"), _make_result("normalization", "ok")]
        svc = self._make_service(results)
        assert svc._overall_status(results) == "ok"

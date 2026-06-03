from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from unittest.mock import patch

from app.auto_healing import scheduler as auto_scheduler
from app.auto_healing.health_checker import HealthChecker
from app.auto_healing.incident_classifier import IncidentClassifier
from app.auto_healing.models import (
    AlertAssessment,
    Classification,
    GeneralStatus,
    OperationalAlert,
    ServiceHealth,
    WatchdogExecution,
)
from app.auto_healing.reporter import AutoHealingReporter
from app.auto_healing.safe_fixes import SafeFixEngine
from app.auto_healing.watchdog import _append_history
from core.config import settings


def _execution() -> WatchdogExecution:
    return WatchdogExecution(
        timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        status=GeneralStatus.DEGRADED,
        dry_run=True,
        alerts_analyzed=[],
        service_health=[],
        actions=[],
        manual_pending=[],
    )


def test_classifier_marks_recovered_when_related_health_is_ok():
    alert = OperationalAlert(
        code="telegram_publish_failing",
        title="Falha Telegram",
        message="Telegram falhando",
        severity="critical",
        source="telegram",
    )
    health = [ServiceHealth("telegram_alerts", "OK", {"events": 3})]

    result = IncidentClassifier().classify([alert], health)

    assert result[0].classification == Classification.RECUPERADO
    assert "telegram_alerts" in result[0].related_health


def test_classifier_marks_duplicate_after_first_occurrence():
    alert = OperationalAlert(
        code="normalization_backlog",
        title="Backlog",
        message="Fila com backlog",
        severity="warning",
        source="normalization",
    )
    health = [ServiceHealth("queues", "DEGRADED", {"pending": 300})]

    result = IncidentClassifier().classify([alert, alert], health)

    assert result[0].classification == Classification.INCONCLUSIVO
    assert result[1].classification == Classification.DUPLICADO


def test_health_checker_postgres_ok():
    db = MagicMock()

    result = HealthChecker(db)._postgres()

    assert result.name == "postgres"
    assert result.status == "OK"
    db.execute.assert_called_once()


def test_safe_fixes_stay_dry_run_and_create_manual_pending_for_real_incident():
    db = MagicMock()
    assessment = AlertAssessment(
        alert=OperationalAlert(
            code="postgres_down",
            title="Postgres down",
            message="database unavailable",
            severity="critical",
        ),
        classification=Classification.REAL,
    )

    actions, manual = SafeFixEngine(db, dry_run=True).apply([assessment], [])

    assert actions[0].dry_run is True
    assert actions[0].status == "dry_run"
    assert "postgres_down" in manual[0]
    db.execute.assert_not_called()


def test_auto_healing_disabled_does_not_execute_watchdog(monkeypatch):
    session_factory = MagicMock()
    monkeypatch.setattr(settings, "auto_healing_enabled", False)
    monkeypatch.setattr(auto_scheduler, "SessionLocal", session_factory)

    auto_scheduler.auto_healing_watchdog_job()

    session_factory.assert_not_called()


def test_dry_run_false_still_does_not_mutate_sensitive_state():
    db = MagicMock()
    assessment = AlertAssessment(
        alert=OperationalAlert(
            code="postgres_down",
            title="Postgres down",
            message="database unavailable",
            severity="critical",
        ),
        classification=Classification.REAL,
    )
    health = [ServiceHealth("postgres", "CRITICAL")]

    actions, manual = SafeFixEngine(db, dry_run=False).apply([assessment], health)

    assert actions[0].target == "postgres"
    db.execute.assert_called_once()
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.delete.assert_not_called()
    assert "postgres_down" in manual[0]


def test_false_positive_does_not_generate_corrective_action():
    db = MagicMock()
    assessment = AlertAssessment(
        alert=OperationalAlert(
            code="transient_warning",
            title="Transient warning",
            message="No longer valid",
            severity="warning",
        ),
        classification=Classification.FALSO_POSITIVO,
    )

    actions, manual = SafeFixEngine(db, dry_run=True).apply([assessment], [ServiceHealth("redis", "OK")])

    assert actions == []
    assert manual == []
    db.execute.assert_not_called()


def test_duplicate_does_not_generate_repeated_action():
    db = MagicMock()
    assessments = [
        AlertAssessment(
            alert=OperationalAlert(
                code="queue_backlog",
                title="Backlog",
                message="Repeated alert",
                severity="warning",
            ),
            classification=Classification.DUPLICADO,
        )
        for _ in range(2)
    ]

    actions, manual = SafeFixEngine(db, dry_run=True).apply(assessments, [ServiceHealth("queues", "OK")])

    assert actions == []
    assert manual == []
    db.execute.assert_not_called()


def test_jsonl_rotates_when_size_exceeds_limit(tmp_path, monkeypatch):
    history = tmp_path / "auto_healing_watchdog.jsonl"
    history.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    monkeypatch.setattr(settings, "auto_healing_history_path", str(history))
    monkeypatch.setattr(settings, "auto_healing_history_max_mb", 1)

    _append_history(_execution())

    assert history.exists()
    assert "AUTO" not in history.read_text(encoding="utf-8")
    assert len(list(tmp_path.glob("auto_healing_watchdog.jsonl.*"))) == 1


def test_jsonl_missing_directory_is_created(tmp_path, monkeypatch):
    history = tmp_path / "missing" / "nested" / "auto_healing_watchdog.jsonl"
    monkeypatch.setattr(settings, "auto_healing_history_path", str(history))
    monkeypatch.setattr(settings, "auto_healing_history_max_mb", 10)

    _append_history(_execution())

    assert history.exists()
    assert history.read_text(encoding="utf-8").strip()


def test_telegram_disabled_does_not_call_notifier(monkeypatch):
    monkeypatch.setattr(settings, "auto_healing_telegram_report", False)
    with patch("app.auto_healing.reporter.TelegramNotifier") as notifier:
        assert AutoHealingReporter().send(_execution()) is False
    notifier.assert_not_called()


def test_telegram_cooldown_blocks_repeated_send(tmp_path, monkeypatch):
    history = tmp_path / "auto_healing_watchdog.jsonl"
    state = tmp_path / "auto_healing_watchdog.jsonl.telegram_state.json"
    state.write_text(
        '{"last_sent_at": "2026-06-01T12:00:00+00:00"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "auto_healing_telegram_report", True)
    monkeypatch.setattr(settings, "auto_healing_history_path", str(history))
    monkeypatch.setattr(settings, "auto_healing_telegram_cooldown_minutes", 120)

    with patch("app.auto_healing.reporter.datetime") as dt, patch(
        "app.auto_healing.reporter.TelegramNotifier"
    ) as notifier:
        dt.now.return_value = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)
        dt.fromisoformat.side_effect = datetime.fromisoformat
        assert AutoHealingReporter().send(_execution()) is False

    notifier.assert_not_called()


def test_telegram_failure_does_not_break_execution(monkeypatch):
    monkeypatch.setattr(settings, "auto_healing_telegram_report", True)
    monkeypatch.setattr(settings, "auto_healing_telegram_cooldown_minutes", 0)
    with patch("app.auto_healing.reporter.TelegramNotifier") as notifier:
        notifier.return_value.send_plain.side_effect = RuntimeError("telegram down")
        assert AutoHealingReporter().send(_execution()) is False


def test_interval_below_minimum_is_clamped(monkeypatch):
    monkeypatch.setattr(settings, "auto_healing_interval_minutes", 1)

    assert auto_scheduler.effective_auto_healing_interval_minutes() == 15


def test_reporter_formats_required_summary_sections():
    execution = WatchdogExecution(
        timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        status=GeneralStatus.DEGRADED,
        dry_run=True,
        alerts_analyzed=[
            AlertAssessment(
                alert=OperationalAlert(
                    code="queue_backlog",
                    title="Backlog",
                    message="Fila degradada",
                    severity="warning",
                ),
                classification=Classification.INCONCLUSIVO,
            )
        ],
        service_health=[ServiceHealth("queues", "DEGRADED")],
        actions=[],
        manual_pending=["queue_backlog: INCONCLUSIVO - Backlog"],
    )

    text = AutoHealingReporter().format(execution)

    assert "AUTO-HEALING WATCHDOG - 2026-06-01" in text
    assert "Status geral:" in text
    assert "- total: 1" in text
    assert "Pendencias manuais:" in text
    assert "Modo: DRY_RUN" in text

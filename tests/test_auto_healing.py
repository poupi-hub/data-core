from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.auto_healing import scheduler as auto_scheduler
from app.auto_healing.healer import (
    BullMQStalledCleaner,
    NormalizationBacklogHealer,
    find_healer,
    run_healers,
)
from app.auto_healing.health_checker import HealthChecker
from app.auto_healing.incident_classifier import IncidentClassifier
from app.auto_healing.models import (
    Classification,
    GeneralStatus,
    HealOutcome,
    HealResult,
    ServiceHealth,
    WatchdogExecution,
)
from app.auto_healing.notifier import CriticalNotifier, should_notify
from app.auto_healing.reporter import AutoHealingReporter
from app.auto_healing.safe_fixes import RecommendationEngine
from app.auto_healing.watchdog import AutoHealingWatchdog, _append_history, _reconcile_outcomes
from core.config import settings

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _execution() -> WatchdogExecution:
    return WatchdogExecution(
        timestamp=NOW,
        status=GeneralStatus.DEGRADED,
        dry_run=True,
        events=[],
        service_health=[],
    )


# --- health checker ---

def test_health_checker_postgres_ok():
    db = MagicMock()
    result = HealthChecker(db)._postgres()
    assert result.name == "postgres"
    assert result.status == "OK"
    db.execute.assert_called_once()


# --- incident classifier ---

def test_single_evidence_does_not_generate_auto_healable():
    health = [ServiceHealth("scheduler", "DEGRADED", {"heartbeat_age_seconds": 901})]
    events = IncidentClassifier().classify([], health, now=NOW)
    assert len(events) == 1
    assert events[0].classification == Classification.OBSERVATION
    assert events[0].evidence_count == 1
    assert events[0].confidence_score == 0.35
    assert events[0].potential_false_positive is True
    assert events[0].action_allowed is False
    assert events[0].dry_run is True


def test_auto_healable_requires_two_evidence_sources():
    health = [
        ServiceHealth(
            "scheduler",
            "DEGRADED",
            {
                "heartbeat_age_seconds": 1200,
                "evidence_sources": ["endpoint", "logs"],
                "probe_cycles": 2,
            },
        )
    ]
    events = IncidentClassifier().classify([], health, now=NOW)
    assert events[0].classification == Classification.AUTO_HEALABLE_DRY_RUN
    assert events[0].confidence_score == 0.80
    assert events[0].evidence_count == 3
    assert events[0].decision == "recommend_action"
    assert events[0].recommended_action == "diagnose_scheduler_heartbeat"
    assert events[0].action_allowed is False


def test_queue_requires_duration_or_trend():
    single_sample = [ServiceHealth("queues", "DEGRADED", {"pending_normalization": 600})]
    sustained = [
        ServiceHealth(
            "queues",
            "DEGRADED",
            {"pending_normalization": 600, "trend_minutes": 10, "evidence_sources": ["redis"]},
        )
    ]
    single_event = IncidentClassifier().classify([], single_sample, now=NOW)[0]
    sustained_event = IncidentClassifier().classify([], sustained, now=NOW)[0]
    assert single_event.classification == Classification.OBSERVATION
    assert sustained_event.classification == Classification.DEGRADED
    assert sustained_event.evidence_count == 2


def test_redundant_sources_are_deduplicated():
    health = [
        ServiceHealth(
            "data-core",
            "DEGRADED",
            {"status_code": 503, "evidence_sources": ["prometheus", "docker"]},
        )
    ]
    event = IncidentClassifier().classify([], health, now=NOW)[0]
    assert event.evidence_sources == ["endpoint"]
    assert event.evidence_count == 1
    assert event.classification == Classification.OBSERVATION


def test_incident_id_stable_across_time():
    health = [ServiceHealth("redis", "CRITICAL", {"ping": False}, "connection refused")]
    first = IncidentClassifier().classify([], health, now=NOW)[0]
    second = IncidentClassifier().classify([], health, now=NOW + timedelta(minutes=5))[0]
    assert first.incident_id == second.incident_id
    assert first.failure_fingerprint == second.failure_fingerprint


def test_cooldown_is_per_incident_id():
    health = [ServiceHealth("redis", "CRITICAL", {"ping": False}, "connection refused")]
    first = IncidentClassifier().classify([], health, now=NOW)[0]
    second = IncidentClassifier().classify(
        [], health, now=NOW + timedelta(minutes=5), previous_events=[first]
    )[0]
    assert first.incident_id == second.incident_id
    assert second.status == "suppressed"
    assert second.potential_false_positive is True


def test_flapping_becomes_potential_false_positive():
    health = [ServiceHealth("redis", "CRITICAL", {"ping": False}, "connection refused")]
    base = IncidentClassifier().classify([], health, now=NOW)[0]
    previous = [
        base,
        base.__class__(**{**base.__dict__, "classification": Classification.DEGRADED}),
        base.__class__(**{**base.__dict__, "classification": Classification.OBSERVATION}),
    ]
    event = IncidentClassifier().classify(
        [], health, now=NOW + timedelta(minutes=45), previous_events=previous
    )[0]
    assert event.potential_false_positive is True


# --- HealOutcome model ---

def test_heal_result_to_dict():
    r = HealResult(service="queues", outcome=HealOutcome.RECOVERED, detail="ok", rows_affected=5)
    d = r.to_dict()
    assert d["outcome"] == "RECOVERED"
    assert d["rows_affected"] == 5


# --- NormalizationBacklogHealer ---

def test_normalization_healer_can_heal_when_failed_nonzero():
    h = NormalizationBacklogHealer()
    item = ServiceHealth("queues", "DEGRADED", {"failed_normalization": 10})
    assert h.can_heal(item) is True


def test_normalization_healer_skips_zero_failed():
    h = NormalizationBacklogHealer()
    item = ServiceHealth("queues", "DEGRADED", {"failed_normalization": 0})
    assert h.can_heal(item) is False


def test_normalization_healer_heal_commits_db():
    h = NormalizationBacklogHealer()
    db = MagicMock()
    execute_result = MagicMock()
    execute_result.rowcount = 3
    db.execute.return_value = execute_result
    result = h.heal(db)
    db.execute.assert_called_once()
    db.commit.assert_called_once()
    assert result.outcome == HealOutcome.RECOVERED
    assert result.rows_affected == 3


def test_normalization_healer_returns_failed_on_db_error():
    h = NormalizationBacklogHealer()
    db = MagicMock()
    db.execute.side_effect = RuntimeError("db error")
    result = h.heal(db)
    db.rollback.assert_called_once()
    assert result.outcome == HealOutcome.FAILED
    assert "db error" in result.error


# --- BullMQStalledCleaner ---

def test_bullmq_healer_can_heal_when_stalled_nonzero():
    h = BullMQStalledCleaner()
    item = ServiceHealth("bullmq", "CRITICAL", {"counts": {"stalled": 2}})
    assert h.can_heal(item) is True


def test_bullmq_healer_skips_zero_stalled():
    h = BullMQStalledCleaner()
    item = ServiceHealth("bullmq", "DEGRADED", {"counts": {"stalled": 0, "failed": 5}})
    assert h.can_heal(item) is False


# --- find_healer ---

def test_find_healer_returns_none_for_scheduler():
    item = ServiceHealth("scheduler", "DEGRADED", {"heartbeat_age_seconds": 1000})
    assert find_healer(item) is None


def test_find_healer_returns_normalization_for_failed_queue():
    item = ServiceHealth("queues", "DEGRADED", {"failed_normalization": 5})
    healer = find_healer(item)
    assert healer is not None
    assert healer.name == "reset_normalization_failed"


# --- run_healers ---

def test_run_healers_skips_healthy_services():
    db = MagicMock()
    health = [ServiceHealth("queues", "OK", {})]
    results = run_healers(health, db)
    assert results == []


def test_run_healers_returns_skipped_for_critical_without_healer():
    db = MagicMock()
    health = [ServiceHealth("scheduler", "CRITICAL", {})]
    results = run_healers(health, db)
    assert len(results) == 1
    assert results[0].outcome == HealOutcome.SKIPPED
    assert results[0].service == "scheduler"


# --- _reconcile_outcomes ---

def test_reconcile_upgrades_recovered_when_still_unhealthy():
    results = [HealResult("queues", HealOutcome.RECOVERED, "reset 5 records", 5)]
    verified = [ServiceHealth("queues", "DEGRADED", {"failed_normalization": 5})]
    reconciled = _reconcile_outcomes(results, verified)
    assert reconciled[0].outcome == HealOutcome.FAILED
    assert "still unhealthy" in reconciled[0].detail


def test_reconcile_keeps_recovered_when_now_healthy():
    results = [HealResult("queues", HealOutcome.RECOVERED, "reset 5 records", 5)]
    verified = [ServiceHealth("queues", "OK", {})]
    reconciled = _reconcile_outcomes(results, verified)
    assert reconciled[0].outcome == HealOutcome.RECOVERED


def test_reconcile_does_not_change_failed_or_skipped():
    results = [
        HealResult("bullmq", HealOutcome.FAILED, "redis error"),
        HealResult("scheduler", HealOutcome.SKIPPED, "no healer"),
    ]
    verified = [
        ServiceHealth("bullmq", "CRITICAL", {}),
        ServiceHealth("scheduler", "CRITICAL", {}),
    ]
    reconciled = _reconcile_outcomes(results, verified)
    assert reconciled[0].outcome == HealOutcome.FAILED
    assert reconciled[1].outcome == HealOutcome.SKIPPED


# --- notifier ---

def test_should_notify_false_for_recovered():
    r = HealResult("queues", HealOutcome.RECOVERED, "ok")
    assert should_notify(r) is False


def test_should_notify_true_for_failed():
    r = HealResult("redis", HealOutcome.FAILED, "err")
    assert should_notify(r) is True


def test_should_notify_true_for_skipped_critical_service():
    r = HealResult("scheduler", HealOutcome.SKIPPED, "no healer")
    assert should_notify(r) is True


def test_should_notify_false_for_skipped_noncritical():
    r = HealResult("telegram_alerts", HealOutcome.SKIPPED, "no healer")
    assert should_notify(r) is False


def test_notifier_silent_when_all_recovered():
    notifier = CriticalNotifier(bot_token="tok", chat_id="123")
    results = [HealResult("queues", HealOutcome.RECOVERED, "ok")]
    with patch.object(notifier, "_send") as mock_send:
        notifier.notify(results, {})
        mock_send.assert_not_called()


def test_notifier_sends_on_failed():
    notifier = CriticalNotifier(bot_token="tok", chat_id="123")
    results = [HealResult("redis", HealOutcome.FAILED, "connection error")]
    with patch.object(notifier, "_send") as mock_send:
        notifier.notify(results, {})
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "redis" in text
        assert "FAILED" in text


# --- watchdog integration ---

def test_auto_healing_disabled_does_not_execute_watchdog(monkeypatch):
    session_factory = MagicMock()
    monkeypatch.setattr(settings, "auto_healing_enabled", False)
    monkeypatch.setattr(auto_scheduler, "SessionLocal", session_factory)
    auto_scheduler.auto_healing_watchdog_job()
    session_factory.assert_not_called()


def test_watchdog_dry_run_does_not_call_healers(monkeypatch, tmp_path):
    history = tmp_path / "auto_healing_watchdog.jsonl"
    monkeypatch.setattr(settings, "auto_healing_history_path", str(history))
    monkeypatch.setattr(settings, "auto_healing_dry_run", True)
    db = MagicMock()

    with (
        patch("app.auto_healing.watchdog.TelegramAlertReader") as reader,
        patch("app.auto_healing.watchdog.HealthChecker") as checker,
        patch("app.auto_healing.watchdog.run_healers") as mock_heal,
    ):
        reader.return_value.recent_alerts.return_value = []
        checker.return_value.run.return_value = [
            ServiceHealth("scheduler", "DEGRADED", {"heartbeat_age_seconds": 901})
        ]
        execution = AutoHealingWatchdog(db).run()

    mock_heal.assert_not_called()
    assert execution.dry_run is True
    assert history.exists()
    payload = json.loads(history.read_text(encoding="utf-8").strip())
    assert payload["dry_run"] is True


def test_watchdog_heal_enabled_calls_healers_and_verifies(monkeypatch, tmp_path):
    history = tmp_path / "auto_healing_watchdog.jsonl"
    monkeypatch.setattr(settings, "auto_healing_history_path", str(history))
    monkeypatch.setattr(settings, "auto_healing_dry_run", False)
    db = MagicMock()

    unhealthy = ServiceHealth("queues", "DEGRADED", {"failed_normalization": 5})
    healthy = ServiceHealth("queues", "OK", {})
    healed = HealResult("queues", HealOutcome.RECOVERED, "reset 5", 5)

    with (
        patch("app.auto_healing.watchdog.TelegramAlertReader") as reader,
        patch("app.auto_healing.watchdog.HealthChecker") as checker,
        patch("app.auto_healing.watchdog.run_healers", return_value=[healed]),
        patch("app.auto_healing.watchdog._notify"),
        patch("app.auto_healing.watchdog.time") as mock_time,
    ):
        mock_time.perf_counter.return_value = 0.0
        mock_time.sleep = MagicMock()
        reader.return_value.recent_alerts.return_value = []
        checker.return_value.run.side_effect = [[unhealthy], [healthy]]
        execution = AutoHealingWatchdog(db).run()

    assert execution.dry_run is False
    assert len(execution.heal_results) == 1
    assert execution.heal_results[0].outcome == HealOutcome.RECOVERED
    mock_time.sleep.assert_called_once_with(5)


# --- existing compatibility ---

def test_reporter_never_sends_telegram():
    assert AutoHealingReporter().send(_execution()) is False


def test_recommendation_engine_never_marks_executed():
    health = [
        ServiceHealth(
            "scheduler",
            "DEGRADED",
            {
                "heartbeat_age_seconds": 1200,
                "evidence_sources": ["endpoint", "logs"],
                "probe_cycles": 2,
            },
        )
    ]
    event = IncidentClassifier().classify([], health, now=NOW)[0]
    recommendations = RecommendationEngine().recommendations([event])
    assert recommendations == ["diagnose_scheduler_heartbeat"]
    assert "executed" not in json.dumps(event.to_dict())


def test_jsonl_rotates_when_size_exceeds_limit(tmp_path, monkeypatch):
    history = tmp_path / "auto_healing_watchdog.jsonl"
    history.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    monkeypatch.setattr(settings, "auto_healing_history_path", str(history))
    monkeypatch.setattr(settings, "auto_healing_history_max_mb", 1)
    _append_history(_execution())
    assert history.exists()
    assert len(list(tmp_path.glob("auto_healing_watchdog.jsonl.*"))) == 1


def test_jsonl_missing_directory_is_created(tmp_path, monkeypatch):
    history = tmp_path / "missing" / "nested" / "auto_healing_watchdog.jsonl"
    monkeypatch.setattr(settings, "auto_healing_history_path", str(history))
    monkeypatch.setattr(settings, "auto_healing_history_max_mb", 10)
    _append_history(_execution())
    assert history.exists()
    assert history.read_text(encoding="utf-8").strip()


def test_interval_below_minimum_is_clamped(monkeypatch):
    monkeypatch.setattr(settings, "auto_healing_interval_minutes", 1)
    assert auto_scheduler.effective_auto_healing_interval_minutes() == 15


def test_reporter_formats_required_summary_sections():
    health = [ServiceHealth("scheduler", "DEGRADED", {"heartbeat_age_seconds": 901})]
    event = IncidentClassifier().classify([], health, now=NOW)[0]
    execution = WatchdogExecution(
        timestamp=NOW,
        status=GeneralStatus.DEGRADED,
        dry_run=True,
        events=[event],
        service_health=health,
    )
    text = AutoHealingReporter().format(execution)
    assert "AUTO-HEALING WATCHDOG - 2026-06-01" in text
    assert "Status geral:" in text
    assert "- total: 1" in text
    assert "Modo: DRY_RUN STRICT" in text
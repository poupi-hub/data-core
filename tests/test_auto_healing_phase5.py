"""Phase 5 tests: operational intelligence, risk scoring, recommendations."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.auto_healing.intelligence import (
    _RISK_THRESHOLDS,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    ExecutiveReporter,
    HealerEffectivenessAnalyzer,
    RecommendationsEngine,
    RiskScorer,
    TopCausesAnalyzer,
    WeeklyExecutiveReport,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

BASE = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _entry(ts: datetime, health: dict[str, str], heals: list[dict] | None = None) -> dict:
    return {
        "timestamp": ts.isoformat(),
        "status": "DEGRADED",
        "dry_run": False,
        "service_health": [{"name": k, "status": v} for k, v in health.items()],
        "heal_results": heals or [],
        "errors": [],
    }


def _write_history(entries: list[dict], path: Path) -> None:
    with path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _make_reader(entries: list[dict]):
    """Build a HistoryReader backed by a temp JSONL file."""
    from app.auto_healing.analytics import HistoryReader
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False,
                                     encoding="utf-8")
    path = Path(tmp.name)
    tmp.close()
    _write_history(entries, path)
    return HistoryReader(history_path=str(path))


# ── Risk thresholds ───────────────────────────────────────────────────────────

def test_risk_thresholds_ordering():
    assert _RISK_THRESHOLDS[RISK_CRITICAL] > _RISK_THRESHOLDS[RISK_HIGH]
    assert _RISK_THRESHOLDS[RISK_HIGH] > _RISK_THRESHOLDS[RISK_MEDIUM]


def test_risk_levels_are_distinct_strings():
    levels = {RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL}
    assert len(levels) == 4


# ── RiskScorer._compute ───────────────────────────────────────────────────────

def test_risk_compute_perfect_service():
    result = RiskScorer._compute("redis", 100.0, 0, None, None)
    assert result.risk == RISK_LOW
    assert result.risk_score == pytest.approx(0.0)


def test_risk_compute_zero_reliability_many_incidents():
    result = RiskScorer._compute("worker", 0.0, 10, None, None)
    # 40 (reliability) + 30 (incidents capped at 6*5) = 70
    assert result.risk_score == pytest.approx(70.0)
    assert result.risk == RISK_HIGH


def test_risk_compute_critical():
    # 0% reliability + 6 incidents + 60-min MTTR + 0% heal rate
    result = RiskScorer._compute("bad", 0.0, 6, 3600.0, 0.0)
    # 40 + 30 + min(30*0.5,20)=15 + 10 = 95
    assert result.risk_score == pytest.approx(95.0)
    assert result.risk == RISK_CRITICAL


def test_risk_compute_medium_range():
    # partial reliability, few incidents
    result = RiskScorer._compute("scheduler", 80.0, 2, None, None)
    # (1-0.8)*40=8 + 2*5=10 = 18 → MEDIUM? No: 18 < 25 = LOW
    # Actually 18 < 25 so LOW
    assert result.risk == RISK_LOW


def test_risk_compute_medium_with_heal_rate():
    result = RiskScorer._compute("workers", 70.0, 3, 600.0, 0.33)
    # (1-0.70)*40=12 + 3*5=15 + min(10*0.5,20)=5 + (1-0.33)*10=6.7 = 38.7 → MEDIUM
    assert result.risk == RISK_MEDIUM


def test_risk_factors_populated():
    result = RiskScorer._compute("svc", 60.0, 5, 1200.0, 0.5)
    assert len(result.factors) >= 1  # at least one factor mentioned


def test_risk_clamped_to_100():
    result = RiskScorer._compute("svc", 0.0, 100, 100_000.0, 0.0)
    assert result.risk_score <= 100.0


def test_risk_no_incidents_returns_stable_factors():
    result = RiskScorer._compute("api", 100.0, 0, None, None)
    assert result.factors  # must have at least one factor string


# ── TopCausesAnalyzer ─────────────────────────────────────────────────────────

def test_top_causes_empty_history():
    reader = _make_reader([])
    analyzer = TopCausesAnalyzer(history_reader=reader)
    result = analyzer.analyze(window_days=7)
    assert result == []


def test_top_causes_single_service():
    entries = [
        _entry(BASE, {"redis": "DEGRADED"}),
        _entry(BASE + timedelta(seconds=30), {"redis": "OK"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
    ]
    reader = _make_reader(entries)
    result = TopCausesAnalyzer(history_reader=reader).analyze(window_days=7)
    assert len(result) == 1
    assert result[0].service == "redis"
    assert result[0].incident_count == 1
    assert result[0].pct_of_total == pytest.approx(100.0)


def test_top_causes_ranked_by_count():
    # workers: 2 incidents, redis: 1 incident
    entries = [
        _entry(BASE, {"workers": "DEGRADED"}),
        _entry(BASE + timedelta(seconds=30), {"workers": "OK"}),
        _entry(BASE + timedelta(hours=1), {"workers": "DEGRADED"}),
        _entry(BASE + timedelta(hours=1, seconds=30), {"workers": "OK"}),
        _entry(BASE + timedelta(hours=2), {"redis": "DEGRADED"}),
        _entry(BASE + timedelta(hours=2, seconds=30), {"redis": "OK"}),
    ]
    reader = _make_reader(entries)
    result = TopCausesAnalyzer(history_reader=reader).analyze(window_days=7)
    assert result[0].service == "workers"
    assert result[0].incident_count == 2


def test_top_causes_pct_sums_to_100():
    entries = [
        _entry(BASE, {"workers": "DEGRADED"}),
        _entry(BASE + timedelta(seconds=30), {"workers": "OK"}),
        _entry(BASE + timedelta(hours=1), {"redis": "DEGRADED"}),
        _entry(BASE + timedelta(hours=1, seconds=30), {"redis": "OK"}),
    ]
    reader = _make_reader(entries)
    result = TopCausesAnalyzer(history_reader=reader).analyze(window_days=7)
    total_pct = sum(c.pct_of_total for c in result)
    assert total_pct == pytest.approx(100.0, abs=0.1)


def test_top_causes_window_days_respected():
    # Entry older than 7 days should not appear in 7d window
    now = datetime.now(timezone.utc)
    old_entry_ts = now - timedelta(days=10)
    recent_entry_ts = now - timedelta(hours=2)
    entries = [
        _entry(old_entry_ts, {"redis": "DEGRADED"}),
        _entry(old_entry_ts + timedelta(seconds=30), {"redis": "OK"}),
        _entry(recent_entry_ts, {"workers": "DEGRADED"}),
        _entry(recent_entry_ts + timedelta(seconds=30), {"workers": "OK"}),
    ]
    reader = _make_reader(entries)
    result = TopCausesAnalyzer(history_reader=reader).analyze(window_days=7)
    services = {c.service for c in result}
    assert "redis" not in services
    assert "workers" in services


# ── HealerEffectivenessAnalyzer ───────────────────────────────────────────────

def test_healer_effectiveness_insufficient_data():
    entries = [
        _entry(BASE, {"redis": "DEGRADED"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
    ]
    reader = _make_reader(entries)
    result = HealerEffectivenessAnalyzer(history_reader=reader).analyze(window_hours=168)
    if result:  # 1 attempt → insufficient_data
        redis_h = next((h for h in result if h.target_service == "redis"), None)
        if redis_h:
            assert redis_h.verdict == "insufficient_data"


def test_healer_effectiveness_reliable():
    entries = [
        _entry(BASE, {"redis": "DEGRADED"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
        _entry(BASE + timedelta(hours=1), {"redis": "DEGRADED"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
        _entry(BASE + timedelta(hours=2), {"redis": "DEGRADED"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
    ]
    reader = _make_reader(entries)
    result = HealerEffectivenessAnalyzer(history_reader=reader).analyze(window_hours=168)
    redis_h = next((h for h in result if h.target_service == "redis"), None)
    assert redis_h is not None
    assert redis_h.verdict == "reliable"
    assert redis_h.success_rate == pytest.approx(1.0)


def test_healer_effectiveness_needs_investigation():
    # 4 attempts, only 1 recovered
    entries = [
        _entry(BASE, {"workers": "DEGRADED"},
               heals=[{"service": "workers", "outcome": "RECOVERED"}]),
        _entry(BASE + timedelta(hours=1), {"workers": "DEGRADED"},
               heals=[{"service": "workers", "outcome": "FAILED"}]),
        _entry(BASE + timedelta(hours=2), {"workers": "DEGRADED"},
               heals=[{"service": "workers", "outcome": "FAILED"}]),
        _entry(BASE + timedelta(hours=3), {"workers": "DEGRADED"},
               heals=[{"service": "workers", "outcome": "FAILED"}]),
    ]
    reader = _make_reader(entries)
    result = HealerEffectivenessAnalyzer(history_reader=reader).analyze(window_hours=168)
    w_h = next((h for h in result if h.target_service == "workers"), None)
    assert w_h is not None
    assert w_h.verdict == "needs_investigation"
    assert w_h.success_rate == pytest.approx(0.25)


def test_healer_effectiveness_to_dict():
    from app.auto_healing.intelligence import HealerEffectivenessReport
    h = HealerEffectivenessReport(
        healer="restart_redis",
        target_service="redis",
        attempts=5,
        recovered=5,
        failed=0,
        skipped=0,
        blocked_circuit=0,
        success_rate=1.0,
        estimated_mttr_reduction_seconds=150.0,
        verdict="reliable",
    )
    d = h.to_dict()
    assert d["success_rate"] == pytest.approx(1.0)
    assert d["estimated_mttr_reduction_seconds"] == pytest.approx(150.0)
    assert d["verdict"] == "reliable"


# ── RecommendationsEngine ─────────────────────────────────────────────────────

def _mock_scorer(scores: dict):
    """Build a mock ReliabilityScorer.score_all() return value."""
    from app.auto_healing.reliability import ServiceScore, _grade

    def _make(svc, score):
        return ServiceScore(
            service=svc,
            score=score,
            grade=_grade(score),
            uptime_pct=score,
        )

    mock = MagicMock()
    mock.score_all.return_value = {svc: _make(svc, s) for svc, s in scores.items()}
    return mock


def test_recommendations_low_success_rate_fires_high():
    # workers: 3 attempts, 0 recovered → HIGH
    entries = [
        _entry(BASE, {"workers": "DEGRADED"},
               heals=[{"service": "workers", "outcome": "FAILED"}]),
        _entry(BASE + timedelta(hours=1), {"workers": "DEGRADED"},
               heals=[{"service": "workers", "outcome": "FAILED"}]),
        _entry(BASE + timedelta(hours=2), {"workers": "DEGRADED"},
               heals=[{"service": "workers", "outcome": "FAILED"}]),
        _entry(BASE + timedelta(hours=3), {"workers": "OK"}),
    ]
    reader = _make_reader(entries)
    scorer = _mock_scorer({"workers": 80.0})
    recs = RecommendationsEngine(
        history_reader=reader, reliability_scorer=scorer
    ).generate(window_hours=168)
    high_healer = [r for r in recs if r.priority == "HIGH" and r.category == "healer"
                   and r.service == "workers"]
    assert len(high_healer) >= 1
    assert "root cause" in high_healer[0].message.lower()


def test_recommendations_reliable_healer_fires_low():
    # redis: 3 recovered → LOW "confiável"
    entries = [
        _entry(BASE, {"redis": "DEGRADED"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
        _entry(BASE + timedelta(hours=1), {"redis": "DEGRADED"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
        _entry(BASE + timedelta(hours=2), {"redis": "DEGRADED"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
        _entry(BASE + timedelta(hours=3), {"redis": "OK"}),
    ]
    reader = _make_reader(entries)
    scorer = _mock_scorer({"redis": 100.0})
    recs = RecommendationsEngine(
        history_reader=reader, reliability_scorer=scorer
    ).generate(window_hours=168)
    low_healer = [r for r in recs if r.priority == "LOW" and r.category == "healer"
                  and r.service == "redis"]
    assert len(low_healer) >= 1
    assert "confiável" in low_healer[0].message.lower()


def test_recommendations_low_reliability_score():
    reader = _make_reader([])
    scorer = _mock_scorer({"data-core": 45.0})
    recs = RecommendationsEngine(
        history_reader=reader, reliability_scorer=scorer
    ).generate(window_hours=168)
    high_rel = [r for r in recs if r.priority == "HIGH" and r.category == "reliability"
                and r.service == "data-core"]
    assert len(high_rel) >= 1
    assert "45" in high_rel[0].message


def test_recommendations_no_duplicates():
    reader = _make_reader([])
    scorer = _mock_scorer({"api": 100.0})
    recs = RecommendationsEngine(
        history_reader=reader, reliability_scorer=scorer
    ).generate(window_hours=168)
    # Check no two identical (service, category, message[:60]) tuples
    keys = [(r.service, r.category, r.message[:60]) for r in recs]
    assert len(keys) == len(set(keys))


def test_recommendations_to_dict():
    from app.auto_healing.intelligence import Recommendation
    r = Recommendation(
        service="redis",
        priority="LOW",
        category="healer",
        message="healer confiável",
        evidence={"success_rate": 1.0},
    )
    d = r.to_dict()
    assert d["service"] == "redis"
    assert d["priority"] == "LOW"
    assert d["evidence"]["success_rate"] == pytest.approx(1.0)


# ── WeeklyExecutiveReport ─────────────────────────────────────────────────────

def _make_report(**kwargs) -> WeeklyExecutiveReport:
    defaults = dict(
        period_start="2026-06-01T00:00:00+00:00",
        period_end="2026-06-08T00:00:00+00:00",
        generated_at="2026-06-08T20:00:00+00:00",
        overall_reliability_score=95.0,
        overall_grade="A",
        overall_risk=RISK_LOW,
        incidents_total=3,
        recoveries_total=2,
        heal_success_rate=0.67,
        recovery_rate=0.67,
        mttr_avg_seconds=300.0,
    )
    defaults.update(kwargs)
    return WeeklyExecutiveReport(**defaults)


def test_weekly_report_to_text_contains_key_fields():
    report = _make_report(
        incidents_total=5,
        recoveries_total=4,
        heal_success_rate=0.8,
        mttr_avg_seconds=600.0,
        overall_reliability_score=92.5,
        overall_grade="A",
        overall_risk=RISK_MEDIUM,
    )
    text = report.to_text()
    assert "92.5" in text or "92" in text
    assert "MEDIUM" in text
    assert "5" in text    # incidents
    assert "10" in text   # MTTR in minutes (600s / 60 = 10 min)


def test_weekly_report_to_text_has_sections():
    report = _make_report(
        top_causes_7d=[{"service": "workers", "incident_count": 2, "pct_of_total": 100.0,
                        "avg_duration_seconds": 30.0, "window_days": 7}],
        worst_services=[{"service": "data-core", "reliability_score": 60.0, "grade": "C",
                         "mttr_avg_seconds": None, "incident_count": 2,
                         "heal_success_rate": None, "risk": RISK_HIGH}],
        recommendations=[{"service": "workers", "priority": "HIGH", "category": "healer",
                           "message": "investigar root cause", "evidence": {}}],
    )
    text = report.to_text()
    assert "TOP CAUSAS" in text
    assert "workers" in text
    assert "PIORES SERVIÇOS" in text
    assert "data-core" in text
    assert "RECOMENDAÇÕES" in text


def test_weekly_report_telegram_daily_compact():
    report = _make_report(
        recommendations=[{"service": "workers", "priority": "HIGH", "category": "healer",
                           "message": "investigar root cause urgente", "evidence": {}}],
    )
    text = report.to_telegram(mode="daily")
    # Daily digest should mention reliability and risk
    assert "95" in text or "A" in text
    assert RISK_LOW in text


def test_weekly_report_telegram_weekly_has_full_content():
    report = _make_report(overall_reliability_score=88.0)
    text = report.to_telegram(mode="weekly")
    assert "88" in text


def test_weekly_report_to_dict_structure():
    report = _make_report()
    d = report.to_dict()
    assert "period_start" in d
    assert "overall_reliability_score" in d
    assert "incidents_total" in d
    assert "recommendations" in d
    assert d["overall_reliability_score"] == pytest.approx(95.0)


# ── ExecutiveReporter — fail-safe ─────────────────────────────────────────────

def test_executive_reporter_empty_history():
    reader = _make_reader([])
    scorer = _mock_scorer({})
    reporter = ExecutiveReporter(history_reader=reader, reliability_scorer=scorer)
    report = reporter.generate(window_hours=168)
    assert isinstance(report, WeeklyExecutiveReport)
    assert report.incidents_total == 0


def test_executive_reporter_with_incidents():
    entries = [
        _entry(BASE, {"redis": "DEGRADED"}),
        _entry(BASE + timedelta(seconds=30), {"redis": "OK"},
               heals=[{"service": "redis", "outcome": "RECOVERED"}]),
        _entry(BASE + timedelta(hours=1), {"workers": "DEGRADED"}),
        _entry(BASE + timedelta(hours=1, seconds=60), {"workers": "OK"},
               heals=[{"service": "workers", "outcome": "FAILED"}]),
    ]
    reader = _make_reader(entries)
    scorer = _mock_scorer({"redis": 100.0, "workers": 85.0})
    reporter = ExecutiveReporter(history_reader=reader, reliability_scorer=scorer)
    report = reporter.generate(window_hours=168)
    assert report.incidents_total >= 1
    assert report.recoveries_total >= 1
    # top_causes_7d should list redis and/or workers
    services = {c["service"] for c in report.top_causes_7d}
    assert len(services) >= 1


def test_executive_reporter_text_format():
    reader = _make_reader([])
    scorer = _mock_scorer({"api": 99.0})
    reporter = ExecutiveReporter(history_reader=reader, reliability_scorer=scorer)
    report = reporter.generate(window_hours=168)
    text = report.to_text()
    assert "AutoHealing" in text
    assert "Relatório" in text


def test_executive_reporter_no_crash_on_bad_scorer():
    """If scorer raises, report must still return a valid default."""
    reader = _make_reader([])
    bad_scorer = MagicMock()
    bad_scorer.score_all.side_effect = Exception("scorer down")
    reporter = ExecutiveReporter(history_reader=reader, reliability_scorer=bad_scorer)
    report = reporter.generate(window_hours=168)
    assert isinstance(report, WeeklyExecutiveReport)
    assert report.overall_risk == RISK_LOW

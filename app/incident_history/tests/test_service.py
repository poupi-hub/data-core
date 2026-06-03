"""
Testes unitários do IncidentHistoryService.
"""

import statistics
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from app.incident_history.models import IncidentHistory, IncidentPattern
from app.incident_history.schemas import IncidentHistoryCreate
from app.incident_history.service import IncidentHistoryService, _normalize_root_cause


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc(**delta):
    return datetime.now(timezone.utc) - timedelta(**delta)


def make_history(
    alert_id="INFRA-001",
    alertname="DataCoreApiDown",
    service="data-core",
    severity="critical",
    root_cause="Redis OOM killed",
    rca_confidence=0.92,
    duration_seconds=300,
    fired_at=None,
    recorded_at=None,
) -> IncidentHistory:
    h = IncidentHistory(
        alert_id=alert_id,
        alertname=alertname,
        service=service,
        severity=severity,
        root_cause=root_cause,
        root_cause_bucket=_normalize_root_cause(root_cause),
        rca_confidence=rca_confidence,
        duration_seconds=duration_seconds,
        fired_at=fired_at or now_utc(hours=1),
        recorded_at=recorded_at or now_utc(minutes=30),
    )
    return h


# ── Root cause normalizer ─────────────────────────────────────────────────────

class TestNormalizeRootCause:
    def test_oom_patterns(self):
        assert _normalize_root_cause("Redis OOM killed") == "oom_kill"
        assert _normalize_root_cause("Out of Memory — process killed") == "oom_kill"
        assert _normalize_root_cause("container memory limit exceeded") == "oom_kill"

    def test_scheduler_patterns(self):
        assert _normalize_root_cause("APScheduler frozen — heartbeat stale") == "scheduler_frozen"
        assert _normalize_root_cause("scheduler restart loop") == "crash_loop"

    def test_redis_patterns(self):
        assert _normalize_root_cause("Redis unavailable on port 6380") == "redis_unavailable"

    def test_database_patterns(self):
        assert _normalize_root_cause("PostgreSQL connection refused") == "database_issue"
        assert _normalize_root_cause("DB lock timeout") == "database_issue"

    def test_pipeline_patterns(self):
        assert _normalize_root_cause("normalization backlog growing") == "pipeline_stalled"
        assert _normalize_root_cause("normalize_job stalled") == "pipeline_stalled"

    def test_schema_change(self):
        assert _normalize_root_cause("VTEX parser broke after schema change") == "schema_change"

    def test_price_corruption(self):
        assert _normalize_root_cause("centavos misparse INCIDENT-2 recurrence") == "price_data_corruption"

    def test_unknown_returns_other(self):
        assert _normalize_root_cause("something completely unrecognized xyz") == "other"

    def test_none_returns_none(self):
        assert _normalize_root_cause(None) is None

    def test_empty_returns_none(self):
        # String vazia é falsy — sem root cause = sem bucket (mesma lógica do None)
        assert _normalize_root_cause("") is None


# ── Service unit tests ────────────────────────────────────────────────────────

class TestIncidentHistoryService:
    def setup_method(self):
        self.service = IncidentHistoryService()

    def _db(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        db.query.return_value.filter_by.return_value.first.return_value = None
        return db

    # ── _aggregate_pattern ────────────────────────────────────────────────────

    def test_aggregate_pattern_creates_new(self):
        db = self._db()
        rows = [
            make_history(duration_seconds=300),
            make_history(duration_seconds=600, root_cause="Redis OOM killed"),
        ]
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = rows
        db.query.return_value.filter_by.return_value.first.return_value = None

        self.service._aggregate_pattern(db, "INFRA-001")

        db.add.assert_called_once()
        added: IncidentPattern = db.add.call_args[0][0]
        assert added.total_occurrences == 2
        assert added.resolved_count == 2
        assert added.is_flapping is False
        assert added.mttr_seconds == 450.0  # mean(300, 600)

    def test_aggregate_pattern_updates_existing(self):
        db = self._db()
        existing = IncidentPattern(
            alert_id="INFRA-001",
            alertname="DataCoreApiDown",
            service="data-core",
            severity="critical",
            total_occurrences=1,
        )
        db.query.return_value.filter_by.return_value.first.return_value = existing
        rows = [make_history(duration_seconds=300)]
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = rows

        self.service._aggregate_pattern(db, "INFRA-001")

        db.add.assert_not_called()
        assert existing.total_occurrences == 1
        assert existing.mttr_seconds == 300.0

    def test_aggregate_pattern_detects_flapping(self):
        db = self._db()
        # 4 incidents in last 24h = flapping
        rows = [
            make_history(recorded_at=now_utc(hours=h), fired_at=now_utc(hours=h+0.1))
            for h in [1, 4, 8, 12]
        ]
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = rows

        self.service._aggregate_pattern(db, "INFRA-001")

        added: IncidentPattern = db.add.call_args[0][0]
        assert added.is_flapping is True

    def test_aggregate_pattern_top_root_causes(self):
        db = self._db()
        rows = [
            make_history(root_cause="Redis OOM killed"),
            make_history(root_cause="Redis OOM killed"),
            make_history(root_cause="DB lock timeout"),
        ]
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = rows

        self.service._aggregate_pattern(db, "INFRA-001")

        added: IncidentPattern = db.add.call_args[0][0]
        assert added.top_root_causes is not None
        assert len(added.top_root_causes) == 2
        assert added.top_root_causes[0]["bucket"] == "oom_kill"
        assert added.top_root_causes[0]["count"] == 2
        assert added.top_root_causes[0]["pct"] == pytest.approx(0.667, abs=0.01)

    def test_aggregate_pattern_calculates_recurrence_interval(self):
        db = self._db()
        base = now_utc(hours=10)
        rows = [
            make_history(fired_at=base),
            make_history(fired_at=base + timedelta(hours=4)),
            make_history(fired_at=base + timedelta(hours=8)),
        ]
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = rows

        self.service._aggregate_pattern(db, "INFRA-001")

        added: IncidentPattern = db.add.call_args[0][0]
        assert added.recurrence_interval_hours == pytest.approx(4.0)

    # ── get_likely_root_cause ─────────────────────────────────────────────────

    def test_get_likely_root_cause_returns_none_if_no_pattern(self):
        db = self._db()
        result = self.service.get_likely_root_cause(db, "INFRA-999")
        assert result is None

    def test_get_likely_root_cause_returns_none_if_insufficient_history(self):
        db = MagicMock()
        pattern = IncidentPattern(
            alert_id="INFRA-001",
            alertname="Test",
            severity="critical",
            resolved_count=2,  # < 3
            top_root_causes=[{"bucket": "oom_kill", "count": 2, "pct": 1.0}],
        )
        db.query.return_value.filter_by.return_value.first.return_value = pattern

        result = self.service.get_likely_root_cause(db, "INFRA-001")
        assert result is None

    def test_get_likely_root_cause_returns_hint(self):
        db = MagicMock()
        pattern = IncidentPattern(
            alert_id="INFRA-001",
            alertname="Test",
            severity="critical",
            resolved_count=5,
            rca_confidence_avg=0.88,
            mttr_seconds=450.0,
            is_flapping=False,
            top_root_causes=[{"bucket": "oom_kill", "count": 4, "pct": 0.8}],
        )
        db.query.return_value.filter_by.return_value.first.return_value = pattern

        result = self.service.get_likely_root_cause(db, "INFRA-001")

        assert result is not None
        assert result["root_cause_bucket"] == "oom_kill"
        assert result["total_resolved"] == 5
        assert result["mttr_seconds"] == 450.0
        assert result["is_flapping"] is False
        assert result["confidence"] == pytest.approx(0.8 * 0.88, abs=0.01)

    # ── record_manual ─────────────────────────────────────────────────────────

    @patch("app.incident_history.service.HISTORY_RECORDS_CREATED")
    def test_record_manual_normalizes_bucket(self, mock_counter):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter_by.return_value.first.return_value = None

        create = IncidentHistoryCreate(
            alert_id="CRYPTO-003",
            alertname="RedisDown",
            severity="critical",
            root_cause="Redis process OOM killed on port 6380",
            rca_confidence=0.95,
        )

        result = self.service.record_manual(db, create)

        db.add.assert_called()
        added = db.add.call_args_list[0][0][0]
        assert added.root_cause_bucket == "oom_kill"
        assert added.rca_confidence == 0.95
        mock_counter.inc.assert_called_once()

    # ── aggregate (integration-level unit test) ───────────────────────────────

    @patch("app.incident_history.service.HISTORY_RECORDS_CREATED")
    @patch("app.incident_history.service.HISTORY_PATTERNS_UPDATED")
    @patch("app.incident_history.service.HISTORY_AGGREGATION_ERRORS")
    @patch("app.incident_history.service.HISTORY_AGGREGATION_DURATION")
    def test_aggregate_processes_resolved_events(self, mock_hist, mock_errors, mock_patterns, mock_created):
        from app.incident_bus.models import IncidentEvent

        db = MagicMock()

        # Simulate: 2 processed events, no existing history
        events = [
            IncidentEvent(
                id=1, fingerprint="fp1", alertname="RedisDown", severity="critical",
                status="resolved", alert_id="CRYPTO-003", service="poupi-crypto",
                processed=True, root_cause="Redis OOM", resolution_notes="Restarted",
                rca_confidence=0.9,
            ),
            IncidentEvent(
                id=2, fingerprint="fp2", alertname="PipelineDead", severity="critical",
                status="resolved", alert_id="PIPE-001", service="data-core",
                processed=True, root_cause="Scheduler frozen",
                rca_confidence=0.85,
            ),
        ]

        # Mock scalars().all() for existing_ids query
        mock_execute = MagicMock()
        mock_execute.scalars.return_value.all.return_value = []
        db.execute.return_value = mock_execute

        # Mock events query
        db.query.return_value.filter.return_value.all.return_value = events

        # Mock pattern queries (no existing)
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter_by.return_value.first.return_value = None

        result = self.service.aggregate(db)

        assert result.new_history_records == 2
        assert result.errors == 0
        db.commit.assert_called_once()

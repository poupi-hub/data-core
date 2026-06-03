"""
Testes unitários do IncidentBusService.

Usa MagicMock para o Session — sem dependência de banco real.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.incident_bus.schemas import AlertmanagerAlert, AlertmanagerWebhook
from app.incident_bus.service import IncidentBusService


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_alert(
    alertname: str = "TestAlert",
    status: str = "firing",
    severity: str = "critical",
    service: str = "poupi-crypto",
    alert_id: str = "INFRA-001",
    fingerprint: str = "abc123",
) -> AlertmanagerAlert:
    return AlertmanagerAlert(
        status=status,
        fingerprint=fingerprint,
        labels={
            "alertname": alertname,
            "severity": severity,
            "service": service,
            "alert_id": alert_id,
            "ai_action": "check_logs,check_health",
            "category": "incident",
        },
        annotations={
            "summary": f"{alertname} is firing",
            "impact": "Service unavailable",
            "possible_cause": "OOM kill",
            "runbook": f"operations/runbooks/{service}/{alert_id}.md",
        },
        startsAt=datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc),
    )


def make_webhook(alerts: list[AlertmanagerAlert]) -> AlertmanagerWebhook:
    return AlertmanagerWebhook(
        version="4",
        status="firing",
        receiver="channel-critical",
        alerts=alerts,
        commonLabels={},
        commonAnnotations={},
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestIncidentBusService:
    def setup_method(self):
        self.service = IncidentBusService()

    def _mock_db(self, existing=None):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = existing
        return db

    @patch("app.incident_bus.service.INCIDENT_RECEIVED")
    @patch("app.incident_bus.service.INCIDENT_PERSISTED")
    @patch("app.incident_bus.service.INCIDENT_ERRORS")
    def test_process_single_firing_alert(self, mock_errors, mock_persisted, mock_received):
        alert = make_alert()
        webhook = make_webhook([alert])
        db = self._mock_db(existing=None)

        persisted, errors = self.service.process_webhook(webhook, db)

        assert len(persisted) == 1
        assert len(errors) == 0
        db.add.assert_called_once()
        db.flush.assert_called()
        mock_received.labels.assert_called_once_with(severity="critical", status="firing")
        mock_persisted.labels.assert_called_once_with(severity="critical", alert_id="INFRA-001")

    @patch("app.incident_bus.service.INCIDENT_RECEIVED")
    @patch("app.incident_bus.service.INCIDENT_PERSISTED")
    @patch("app.incident_bus.service.INCIDENT_ERRORS")
    def test_process_resolved_alert(self, mock_errors, mock_persisted, mock_received):
        alert = make_alert(status="resolved")
        alert.endsAt = datetime(2026, 6, 3, 10, 15, 0, tzinfo=timezone.utc)
        webhook = make_webhook([alert])
        db = self._mock_db(existing=None)

        persisted, errors = self.service.process_webhook(webhook, db)

        assert len(persisted) == 1
        added = db.add.call_args[0][0]
        assert added.status == "resolved"

    @patch("app.incident_bus.service.INCIDENT_RECEIVED")
    @patch("app.incident_bus.service.INCIDENT_PERSISTED")
    @patch("app.incident_bus.service.INCIDENT_ERRORS")
    def test_upsert_existing_event(self, mock_errors, mock_persisted, mock_received):
        """Se fingerprint+status já existe, atualiza received_at em vez de criar novo."""
        from app.incident_bus.models import IncidentEvent
        existing = IncidentEvent(
            fingerprint="abc123",
            status="firing",
            alertname="TestAlert",
            severity="critical",
        )
        alert = make_alert(fingerprint="abc123")
        webhook = make_webhook([alert])
        db = self._mock_db(existing=existing)

        persisted, errors = self.service.process_webhook(webhook, db)

        assert len(persisted) == 1
        db.add.assert_not_called()  # não deve criar novo
        assert persisted[0] is existing

    @patch("app.incident_bus.service.INCIDENT_RECEIVED")
    @patch("app.incident_bus.service.INCIDENT_PERSISTED")
    @patch("app.incident_bus.service.INCIDENT_ERRORS")
    def test_multiple_alerts_in_webhook(self, mock_errors, mock_persisted, mock_received):
        alerts = [
            make_alert(alertname=f"Alert{i}", fingerprint=f"fp{i}")
            for i in range(3)
        ]
        webhook = make_webhook(alerts)
        db = self._mock_db(existing=None)

        persisted, errors = self.service.process_webhook(webhook, db)

        assert len(persisted) == 3
        assert len(errors) == 0

    @patch("app.incident_bus.service.INCIDENT_RECEIVED")
    @patch("app.incident_bus.service.INCIDENT_PERSISTED")
    @patch("app.incident_bus.service.INCIDENT_ERRORS")
    def test_db_error_captured_in_errors(self, mock_errors, mock_persisted, mock_received):
        alert = make_alert()
        webhook = make_webhook([alert])
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = RuntimeError("DB down")

        persisted, errors = self.service.process_webhook(webhook, db)

        assert len(persisted) == 0
        assert len(errors) == 1
        assert "DB down" in errors[0]
        mock_errors.labels.assert_called_once()

    def test_build_create_extracts_labels(self):
        alert = make_alert(alert_id="CRYPTO-003", service="poupi-crypto")
        webhook = make_webhook([alert])
        create = self.service._build_create(alert, webhook)

        assert create.alert_id == "CRYPTO-003"
        assert create.service == "poupi-crypto"
        assert create.severity == "critical"
        assert create.status == "firing"
        assert create.ai_action == "check_logs,check_health"
        assert create.runbook is not None
        assert create.impact == "Service unavailable"

    def test_synthetic_fingerprint_is_deterministic(self):
        labels = {"alertname": "Test", "service": "svc", "severity": "critical"}
        fp1 = self.service._synthetic_fingerprint(labels)
        fp2 = self.service._synthetic_fingerprint(labels)
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_mark_processed(self):
        from app.incident_bus.models import IncidentEvent
        event = IncidentEvent(id=1, fingerprint="fp", alertname="A", severity="critical", status="firing")
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = event

        result = self.service.mark_processed(
            db,
            event_id=1,
            root_cause="Redis OOM killed",
            rca_confidence=0.92,
            resolution_notes="Restarted Redis pod",
        )

        assert result.processed is True
        assert result.root_cause == "Redis OOM killed"
        assert result.rca_confidence == 0.92

    def test_get_recent_no_filters(self):
        db = MagicMock()
        db.query.return_value.filter.return_value = db.query.return_value
        db.query.return_value.order_by.return_value.limit.return_value.all.return_value = []

        result = self.service.get_recent(db)
        assert result == []

    def test_get_unprocessed(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        result = self.service.get_unprocessed(db)
        assert result == []

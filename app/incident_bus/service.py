"""
IncidentBusService — converte payloads do Alertmanager em IncidentEvents
e os persiste no PostgreSQL.

Responsabilidades:
  1. Parsear AlertmanagerWebhook → IncidentEventCreate (por alerta)
  2. Enriquecer com metadados canônicos (alert_id, ai_action, runbook)
  3. Persistir na tabela incident_events
  4. Emitir métricas Prometheus
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.incident_bus.metrics import (
    INCIDENT_ERRORS,
    INCIDENT_PERSISTED,
    INCIDENT_RECEIVED,
)
from app.incident_bus.models import IncidentEvent
from app.incident_bus.schemas import (
    AlertmanagerAlert,
    AlertmanagerWebhook,
    IncidentEventCreate,
    IncidentEventRead,
)

logger = logging.getLogger(__name__)


class IncidentBusService:

    def process_webhook(
        self, webhook: AlertmanagerWebhook, db: Session
    ) -> tuple[list[IncidentEvent], list[str]]:
        """
        Processa um webhook do Alertmanager.

        Retorna: (eventos_persistidos, erros)
        """
        persisted: list[IncidentEvent] = []
        errors: list[str] = []

        for alert in webhook.alerts:
            INCIDENT_RECEIVED.labels(
                severity=alert.labels.get("severity", "unknown"),
                status=alert.status,
            ).inc()
            try:
                event = self._persist_alert(alert, webhook, db)
                persisted.append(event)
                INCIDENT_PERSISTED.labels(
                    severity=alert.labels.get("severity", "unknown"),
                    alert_id=alert.labels.get("alert_id", "unknown"),
                ).inc()
            except Exception as exc:
                msg = f"{alert.labels.get('alertname', '?')}: {exc}"
                errors.append(msg)
                INCIDENT_ERRORS.labels(
                    alertname=alert.labels.get("alertname", "unknown"),
                ).inc()
                logger.exception("Failed to persist incident event: %s", msg)

        return persisted, errors

    # ── Private helpers ───────────────────────────────────────────────────────

    def _persist_alert(
        self,
        alert: AlertmanagerAlert,
        webhook: AlertmanagerWebhook,
        db: Session,
    ) -> IncidentEvent:
        create = self._build_create(alert, webhook)

        # Upsert: se fingerprint + status já existe, atualiza; senão cria
        existing = (
            db.query(IncidentEvent)
            .filter_by(fingerprint=create.fingerprint, status=create.status)
            .first()
        )
        if existing:
            # Alerta re-disparou — atualizar received_at
            existing.received_at = datetime.now(timezone.utc)
            existing.raw_payload = create.raw_payload
            db.flush()
            return existing

        event = IncidentEvent(
            fingerprint=create.fingerprint,
            alert_id=create.alert_id,
            alertname=create.alertname,
            service=create.service,
            severity=create.severity,
            category=create.category,
            channel=create.channel,
            component=create.component,
            layer=create.layer,
            runtime=create.runtime,
            ai_action=create.ai_action,
            runbook=create.runbook,
            status=create.status,
            summary=create.summary,
            impact=create.impact,
            possible_cause=create.possible_cause,
            labels=create.labels,
            annotations=create.annotations,
            raw_payload=create.raw_payload,
            fired_at=create.fired_at,
            resolved_at=create.resolved_at,
            processed=False,
        )
        db.add(event)
        db.flush()
        return event

    def _build_create(
        self,
        alert: AlertmanagerAlert,
        webhook: AlertmanagerWebhook,
    ) -> IncidentEventCreate:
        labels = {**webhook.commonLabels, **alert.labels}
        annotations = {**webhook.commonAnnotations, **alert.annotations}

        fired_at = alert.startsAt
        resolved_at = alert.endsAt if alert.status == "resolved" else None

        duration_seconds: int | None = None
        if fired_at and resolved_at:
            duration_seconds = int((resolved_at - fired_at).total_seconds())

        return IncidentEventCreate(
            fingerprint=alert.fingerprint or self._synthetic_fingerprint(labels),

            # Alert metadata from labels
            alert_id=labels.get("alert_id"),
            alertname=labels.get("alertname", "unknown"),
            service=labels.get("service"),
            severity=labels.get("severity", "unknown"),
            category=labels.get("category"),
            channel=labels.get("channel"),
            component=labels.get("component"),
            layer=labels.get("layer"),
            runtime=labels.get("runtime"),

            # AI metadata
            ai_action=labels.get("ai_action"),
            runbook=annotations.get("runbook"),

            status=alert.status,

            # Human context
            summary=annotations.get("summary"),
            impact=annotations.get("impact"),
            possible_cause=annotations.get("possible_cause"),

            # Raw data
            labels=labels,
            annotations=annotations,
            raw_payload={
                "version": webhook.version,
                "groupKey": webhook.groupKey,
                "receiver": webhook.receiver,
                "groupLabels": webhook.groupLabels,
                "externalURL": webhook.externalURL,
            },

            fired_at=fired_at,
            resolved_at=resolved_at,
        )

    @staticmethod
    def _synthetic_fingerprint(labels: dict) -> str:
        """Gera fingerprint quando Alertmanager não envia um."""
        import hashlib
        key = f"{labels.get('alertname','')}-{labels.get('service','')}-{labels.get('severity','')}"
        return hashlib.md5(key.encode()).hexdigest()[:16]  # noqa: S324

    # ── Query helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def get_recent(
        db: Session,
        limit: int = 50,
        severity: str | None = None,
        service: str | None = None,
        status: str | None = None,
    ) -> list[IncidentEvent]:
        q = db.query(IncidentEvent)
        if severity:
            q = q.filter(IncidentEvent.severity == severity)
        if service:
            q = q.filter(IncidentEvent.service == service)
        if status:
            q = q.filter(IncidentEvent.status == status)
        return q.order_by(IncidentEvent.received_at.desc()).limit(limit).all()

    @staticmethod
    def get_unprocessed(db: Session, limit: int = 20) -> list[IncidentEvent]:
        """Para o AI Incident Agent (Fase 10)."""
        return (
            db.query(IncidentEvent)
            .filter(IncidentEvent.processed.is_(False))
            .order_by(IncidentEvent.received_at.asc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def mark_processed(
        db: Session,
        event_id: int,
        root_cause: str | None = None,
        rca_confidence: float | None = None,
        resolution_notes: str | None = None,
    ) -> IncidentEvent | None:
        """Atualizado pelo RCA Engine / AI Agent após diagnóstico."""
        event = db.query(IncidentEvent).filter_by(id=event_id).first()
        if not event:
            return None
        event.processed = True
        event.root_cause = root_cause
        event.rca_confidence = rca_confidence
        event.resolution_notes = resolution_notes
        db.flush()
        return event

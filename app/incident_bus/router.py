"""
Incident Event Bus — API Router

Endpoints:
  POST /api/v1/incidents/webhook   ← Alertmanager webhook receiver
  GET  /api/v1/incidents/          ← Listar eventos recentes
  GET  /api/v1/incidents/unprocessed ← Para o AI Agent (Fase 10)
  GET  /api/v1/incidents/{id}      ← Evento específico
  PATCH /api/v1/incidents/{id}/rca ← Registrar resultado do RCA (Fase 9/10)
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.incident_bus.schemas import (
    AlertmanagerWebhook,
    IncidentEventList,
    IncidentEventRead,
    WebhookResponse,
)
from app.incident_bus.service import IncidentBusService
from database.session import SessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/incidents", tags=["incident-bus"])

_service = IncidentBusService()


def _get_db():
    with SessionLocal() as db:
        yield db


DbDep = Annotated[Session, Depends(_get_db)]


# ── Webhook receiver (Alertmanager → data-core) ───────────────────────────────

@router.post("/webhook", response_model=WebhookResponse, status_code=200)
def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    db: DbDep,
) -> WebhookResponse:
    """
    Receiver para webhooks do Alertmanager.
    Persiste cada alerta como um IncidentEvent na tabela incident_events.
    """
    try:
        persisted, errors = _service.process_webhook(payload, db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Incident bus webhook failed (transaction rolled back)")
        raise HTTPException(status_code=500, detail="Internal error persisting incidents")

    return WebhookResponse(
        received=len(payload.alerts),
        persisted=len(persisted),
        errors=len(errors),
        event_ids=[e.id for e in persisted],
    )


# ── Query endpoints ───────────────────────────────────────────────────────────

@router.get("/", response_model=IncidentEventList)
def list_incidents(
    db: DbDep,
    severity: str | None = Query(None, description="Filtrar por severidade"),
    service:  str | None = Query(None, description="Filtrar por serviço"),
    status:   str | None = Query(None, description="firing | resolved"),
    limit:    int        = Query(50, ge=1, le=500),
) -> IncidentEventList:
    """Lista eventos de incidente recentes."""
    items = _service.get_recent(
        db, limit=limit, severity=severity, service=service, status=status
    )
    return IncidentEventList(total=len(items), items=items)


@router.get("/unprocessed", response_model=IncidentEventList)
def list_unprocessed(
    db: DbDep,
    limit: int = Query(20, ge=1, le=100),
) -> IncidentEventList:
    """
    Eventos ainda não processados pelo RCA Engine.
    Usado pelo AI Incident Agent (Fase 10).
    """
    items = _service.get_unprocessed(db, limit=limit)
    return IncidentEventList(total=len(items), items=items)


@router.get("/{event_id}", response_model=IncidentEventRead)
def get_incident(event_id: int, db: DbDep) -> IncidentEventRead:
    """Retorna um IncidentEvent específico pelo ID."""
    from app.incident_bus.models import IncidentEvent
    event = db.query(IncidentEvent).filter_by(id=event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail=f"Incident {event_id} not found")
    return IncidentEventRead.model_validate(event)


# ── RCA update (Fase 9 / Fase 10) ────────────────────────────────────────────

class RcaUpdate:
    def __init__(
        self,
        root_cause: str | None = None,
        rca_confidence: float | None = None,
        resolution_notes: str | None = None,
    ):
        self.root_cause = root_cause
        self.rca_confidence = rca_confidence
        self.resolution_notes = resolution_notes


from pydantic import BaseModel as _BM


class RcaUpdateRequest(_BM):
    root_cause: str | None = None
    rca_confidence: float | None = None
    resolution_notes: str | None = None


@router.patch("/{event_id}/rca", response_model=IncidentEventRead)
def update_rca(
    event_id: int,
    body: RcaUpdateRequest,
    db: DbDep,
) -> IncidentEventRead:
    """
    Registra o resultado do RCA Engine ou do AI Incident Agent.
    Marca o evento como processado.
    """
    event = _service.mark_processed(
        db,
        event_id=event_id,
        root_cause=body.root_cause,
        rca_confidence=body.rca_confidence,
        resolution_notes=body.resolution_notes,
    )
    if not event:
        raise HTTPException(status_code=404, detail=f"Incident {event_id} not found")
    db.commit()
    return IncidentEventRead.model_validate(event)

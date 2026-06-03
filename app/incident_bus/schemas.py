"""
Schemas para o Incident Event Bus.

Fluxo:
  Alertmanager webhook → AlertmanagerWebhook (parse)
  → IncidentEventCreate (persist)
  → IncidentEventRead (API response)
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Alertmanager webhook payload ──────────────────────────────────────────────

class AlertmanagerAlert(BaseModel):
    """Um único alerta dentro do payload do Alertmanager."""
    status: str                               # "firing" | "resolved"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime | None = None
    endsAt: datetime | None = None
    fingerprint: str = ""
    generatorURL: str = ""


class AlertmanagerWebhook(BaseModel):
    """Payload completo enviado pelo Alertmanager via webhook."""
    version: str = "4"
    groupKey: str = ""
    truncatedAlerts: int = 0
    status: str                               # "firing" | "resolved"
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)


# ── Internal schemas ──────────────────────────────────────────────────────────

class IncidentEventCreate(BaseModel):
    """Schema para criação de um IncidentEvent no banco."""
    fingerprint: str

    alert_id:  str | None = None
    alertname: str
    service:   str | None = None
    severity:  str
    category:  str | None = None
    channel:   str | None = None
    component: str | None = None
    layer:     str | None = None
    runtime:   str | None = None

    ai_action: str | None = None
    runbook:   str | None = None

    status: str

    summary:        str | None = None
    impact:         str | None = None
    possible_cause: str | None = None

    labels:      dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    raw_payload: dict[str, Any] | None = None

    fired_at:    datetime | None = None
    resolved_at: datetime | None = None


class IncidentEventRead(BaseModel):
    """Schema de resposta da API."""
    id: int
    fingerprint: str

    alert_id:  str | None
    alertname: str
    service:   str | None
    severity:  str
    category:  str | None
    channel:   str | None
    ai_action: str | None
    runbook:   str | None

    status: str

    summary:        str | None
    impact:         str | None
    possible_cause: str | None

    fired_at:         datetime | None
    resolved_at:      datetime | None
    duration_seconds: int | None
    received_at:      datetime

    root_cause:     str | None
    rca_confidence: float | None

    processed: bool

    model_config = {"from_attributes": True}


class IncidentEventList(BaseModel):
    total: int
    items: list[IncidentEventRead]


class WebhookResponse(BaseModel):
    """Resposta do endpoint de webhook."""
    received: int        # quantos alertas foram processados
    persisted: int       # quantos foram persistidos com sucesso
    errors: int          # quantos falharam
    event_ids: list[int] # IDs dos eventos criados

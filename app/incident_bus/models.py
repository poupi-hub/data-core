from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # ── Alert metadata ────────────────────────────────────────────────────────
    alert_id:  Mapped[str | None] = mapped_column(String(32),  nullable=True, index=True)
    alertname: Mapped[str]        = mapped_column(String(128), nullable=False)
    service:   Mapped[str | None] = mapped_column(String(64),  nullable=True)
    severity:  Mapped[str]        = mapped_column(String(16),  nullable=False)
    category:  Mapped[str | None] = mapped_column(String(32),  nullable=True)
    channel:   Mapped[str | None] = mapped_column(String(32),  nullable=True)
    component: Mapped[str | None] = mapped_column(String(64),  nullable=True)
    layer:     Mapped[str | None] = mapped_column(String(64),  nullable=True)
    runtime:   Mapped[str | None] = mapped_column(String(32),  nullable=True)

    # ── AI metadata ───────────────────────────────────────────────────────────
    ai_action: Mapped[str | None] = mapped_column(String(256), nullable=True)
    runbook:   Mapped[str | None] = mapped_column(String(256), nullable=True)

    # ── Event status ──────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # firing | resolved

    # ── Human-readable context ────────────────────────────────────────────────
    summary:        Mapped[str | None] = mapped_column(Text, nullable=True)
    impact:         Mapped[str | None] = mapped_column(Text, nullable=True)
    possible_cause: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Raw payload ───────────────────────────────────────────────────────────
    labels:      Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ── Timing ────────────────────────────────────────────────────────────────
    fired_at:         Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None]      = mapped_column(Integer, nullable=True)
    received_at:      Mapped[datetime]        = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ── RCA fields (Phase 9) ──────────────────────────────────────────────────
    root_cause:       Mapped[str | None]   = mapped_column(Text,  nullable=True)
    rca_confidence:   Mapped[float | None] = mapped_column(Float, nullable=True)
    rca_hypothesis:   Mapped[str | None]   = mapped_column(Text,  nullable=True)
    resolution_notes: Mapped[str | None]   = mapped_column(Text,  nullable=True)

    # ── Processing state ──────────────────────────────────────────────────────
    processed:        Mapped[bool]         = mapped_column(Boolean, nullable=False, default=False)
    processing_error: Mapped[str | None]   = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<IncidentEvent {self.alert_id or self.alertname} [{self.status}] @ {self.received_at}>"

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class IncidentHistory(Base):
    """Registro permanente de cada incidente resolvido com RCA confirmado."""
    __tablename__ = "incident_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    incident_event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("incident_events.id", ondelete="SET NULL"), nullable=True
    )

    # Identidade
    alert_id:  Mapped[str | None] = mapped_column(String(32),  nullable=True, index=True)
    alertname: Mapped[str]        = mapped_column(String(128), nullable=False)
    service:   Mapped[str | None] = mapped_column(String(64),  nullable=True)
    severity:  Mapped[str]        = mapped_column(String(16),  nullable=False)
    category:  Mapped[str | None] = mapped_column(String(32),  nullable=True)

    # Root cause
    root_cause:        Mapped[str | None]   = mapped_column(Text,        nullable=True)
    root_cause_bucket: Mapped[str | None]   = mapped_column(String(64),  nullable=True)
    rca_confidence:    Mapped[float | None] = mapped_column(Float,       nullable=True)

    # Resolução
    resolution:      Mapped[str | None] = mapped_column(Text,       nullable=True)
    resolution_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_by:     Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Timing
    fired_at:         Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None]      = mapped_column(Integer, nullable=True)
    recorded_at:      Mapped[datetime]        = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # AI metadata
    ai_action_used:   Mapped[str | None]              = mapped_column(String(256), nullable=True)
    runbook:          Mapped[str | None]               = mapped_column(String(256), nullable=True)
    context_snapshot: Mapped[dict[str, Any] | None]   = mapped_column(JSONB,       nullable=True)


class IncidentPattern(Base):
    """Agregação por alert_id — memória operacional para o RCA Engine."""
    __tablename__ = "incident_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Chave
    alert_id:  Mapped[str]        = mapped_column(String(32),  nullable=False, unique=True, index=True)
    alertname: Mapped[str]        = mapped_column(String(128), nullable=False)
    service:   Mapped[str | None] = mapped_column(String(64),  nullable=True)
    severity:  Mapped[str]        = mapped_column(String(16),  nullable=False)

    # Frequência
    total_occurrences: Mapped[int]          = mapped_column(Integer, nullable=False, default=0)
    resolved_count:    Mapped[int]          = mapped_column(Integer, nullable=False, default=0)
    unresolved_count:  Mapped[int]          = mapped_column(Integer, nullable=False, default=0)
    last_fired_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_fired_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # MTTR
    mttr_seconds:     Mapped[float | None] = mapped_column(Float, nullable=True)
    mttr_p50_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    mttr_p90_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Root causes top-3
    top_root_causes: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)

    # Padrão de recorrência
    recurrence_interval_hours: Mapped[float | None] = mapped_column(Float,    nullable=True)
    is_flapping:               Mapped[bool]         = mapped_column(Boolean,  nullable=False, default=False)
    rca_confidence_avg:        Mapped[float | None] = mapped_column(Float,    nullable=True)

    last_aggregated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<IncidentPattern {self.alert_id} "
            f"occurrences={self.total_occurrences} "
            f"mttr={self.mttr_seconds}s>"
        )

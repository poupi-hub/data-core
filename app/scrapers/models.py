"""SQLAlchemy model for scraper_drift_events table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class ScraperDriftEvent(Base):
    """Records a detected structural or semantic drift in scraper payloads.

    Each row represents a single detected anomaly for a (source_name, collector_name)
    pair at a point in time.  Events are created by StructuralDriftDetector and can be
    resolved manually or automatically when the drift is no longer detected.
    """

    __tablename__ = "scraper_drift_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identity
    source_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    collector_name: Mapped[str] = mapped_column(String(128), nullable=False)
    module: Mapped[str] = mapped_column(String(64), nullable=False)

    # Classification
    drift_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "field_missing | field_added | type_changed | "
            "price_zero | availability_unknown | strategy_fallback"
        ),
    )
    risk_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="low | medium | high | critical",
    )

    # Human-readable description
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    field_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Payload signatures (JSON snapshots of the relevant payload fragment)
    prev_signature: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    curr_signature: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Lifecycle
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<ScraperDriftEvent id={self.id} source={self.source_name!r} "
            f"type={self.drift_type!r} risk={self.risk_level!r}>"
        )

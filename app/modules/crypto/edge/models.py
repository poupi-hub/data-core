"""SQLAlchemy model for multi-horizon signal edge outcomes.

Each row records the price movement N hours after a BUY/SELL signal,
capturing MFE, MAE, and direction correctness at 3 standardised horizons:
  24h · 72h · 168h (7 days)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class SignalEdgeOutcome(Base):
    """Multi-horizon outcome evaluation for a single BUY/SELL signal."""

    __tablename__ = "trading_edge_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # FK to trading_analytics (nullable — survives parent deletion)
    analytics_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trading_analytics.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Evaluation horizon in hours (24 | 72 | 168)
    horizon_hours: Mapped[int] = mapped_column(Integer, nullable=False)

    # Signal context (denormalised for query efficiency)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(20), nullable=False)
    signal: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    regime: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Entry
    signal_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    signal_price: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)

    # Exit at horizon
    outcome_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_price: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    candles_elapsed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Return metrics
    price_change_pct: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    outcome_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

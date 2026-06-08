"""SQLAlchemy model for forward shadow signal tracking — Phase 8."""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.models import Base


class ForwardShadowSignal(Base):
    """One row per analytics signal that matches the shadow forward filter.

    Filter: signal=BUY, regime=UNKNOWN, confidence in [75, 84].
    Outcomes computed for 24h / 72h / 168h horizons.
    """

    __tablename__ = "forward_shadow_signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    analytics_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trading_analytics.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    signal_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signal_price: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)

    # 24-hour outcome
    return_24h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)
    outcome_correct_24h: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    outcome_at_24h: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mfe_24h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)
    mae_24h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)

    # 72-hour outcome
    return_72h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)
    outcome_correct_72h: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    outcome_at_72h: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mfe_72h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)
    mae_72h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)

    # 168-hour outcome
    return_168h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)
    outcome_correct_168h: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    outcome_at_168h: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mfe_168h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)
    mae_168h: Mapped[Numeric | None] = mapped_column(Numeric(10, 4), nullable=True)

    # Alert state
    alert_entry_sent: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    alert_24h_sent: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    alert_72h_sent: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    alert_168h_sent: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

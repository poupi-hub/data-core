"""SQLAlchemy models for operational watchdog tables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class WatchdogRun(Base):
    """Records each execution of the operational_watchdog_job.

    Stores overall status, per-check results, alert codes fired, and whether
    a Telegram notification was sent.
    """

    __tablename__ = "watchdog_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    overall_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="ok | warning | critical"
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # JSON payloads
    check_results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    alert_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    metrics_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<WatchdogRun id={self.id} status={self.overall_status!r} run_at={self.run_at}>"


class TelegramPublicationEvent(Base):
    """Records Telegram publication events reported by poupi-baby.

    poupi-baby calls POST /api/v1/watchdog/report/telegram-published after each
    TelegramGroupProcessor execution (sent or failed).  This gives data-core
    visibility into the last successful publication without direct DB access.
    """

    __tablename__ = "telegram_publication_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identifiers from poupi-baby
    group_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    offer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    marketplace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    deal_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # "sent" | "failed" | "rate_limited" | "skipped"
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    reported_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="poupi-baby service identifier"
    )

    def __repr__(self) -> str:
        return (
            f"<TelegramPublicationEvent id={self.id} status={self.status!r} "
            f"marketplace={self.marketplace!r} published_at={self.published_at}>"
        )

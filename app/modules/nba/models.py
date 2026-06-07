import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from database.models import Base


class PickType(str, enum.Enum):
    moneyline = "moneyline"
    spread = "spread"
    total = "total"
    player_prop = "player_prop"


class PickStatus(str, enum.Enum):
    pending = "pending"
    won = "won"
    lost = "lost"
    void = "void"


class SourceType(str, enum.Enum):
    telegram = "telegram"
    discord = "discord"
    x = "x"
    manual = "manual"


class NbaSource(Base):
    __tablename__ = "nba_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType), index=True)
    handle: Mapped[str | None] = mapped_column(String(160), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    picks: Mapped[list["NbaPick"]] = relationship(back_populates="source")


class NbaPick(Base):
    __tablename__ = "nba_picks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("nba_sources.id"), index=True)  # noqa: E501
    raw_text: Mapped[str] = mapped_column(Text)
    pick_type: Mapped[PickType] = mapped_column(Enum(PickType), index=True)
    team: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    player: Mapped[str | None] = mapped_column(String(160), nullable=True)
    line: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    odd: Mapped[float] = mapped_column(Numeric(8, 4))
    event_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    league: Mapped[str] = mapped_column(String(80), default="NBA", index=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)  # noqa: E501
    parse_status: Mapped[str] = mapped_column(String(40), default="ok", index=True)

    source: Mapped[NbaSource] = relationship(back_populates="picks")
    paper_bets: Mapped[list["NbaPaperBet"]] = relationship(back_populates="pick")

    __table_args__ = (Index("ix_nba_picks_source_captured", "source_id", "captured_at"),)


class NbaPaperBet(Base):
    __tablename__ = "nba_paper_bets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pick_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("nba_picks.id"), unique=True, index=True)  # noqa: E501
    stake: Mapped[float] = mapped_column(Numeric(10, 4), default=1.0)
    status: Mapped[PickStatus] = mapped_column(Enum(PickStatus), default=PickStatus.pending, index=True)  # noqa: E501
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    pick: Mapped[NbaPick] = relationship(back_populates="paper_bets")


class NbaResult(Base):
    __tablename__ = "nba_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_description: Mapped[str] = mapped_column(String(255), index=True)
    home_team: Mapped[str] = mapped_column(String(160), index=True)
    away_team: Mapped[str] = mapped_column(String(160), index=True)
    home_score: Mapped[int | None] = mapped_column(nullable=True)
    away_score: Mapped[int | None] = mapped_column(nullable=True)
    total_points: Mapped[float | None] = mapped_column(Numeric(8, 1), nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("home_team", "away_team", "event_time", name="uq_nba_result_matchup"),)  # noqa: E501

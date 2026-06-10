import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from database.models import Base


class GameStatus(str, enum.Enum):
    scheduled = "scheduled"
    live = "live"
    final = "final"


class MarketType(str, enum.Enum):
    moneyline = "moneyline"
    spread = "spread"
    totals = "totals"


class SignalDirection(str, enum.Enum):
    home = "home"
    away = "away"
    over = "over"
    under = "under"


class BetStatus(str, enum.Enum):
    pending = "pending"
    won = "won"
    lost = "lost"
    void = "void"


class EdgeClassification(str, enum.Enum):
    profitable = "PROFITABLE"
    neutral = "NEUTRAL"
    losing = "LOSING"


class NbaGame(Base):
    __tablename__ = "nba_games"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    game_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    home_team: Mapped[str] = mapped_column(String(160), index=True)
    away_team: Mapped[str] = mapped_column(String(160), index=True)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[GameStatus] = mapped_column(
        Enum(GameStatus), default=GameStatus.scheduled, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    odds: Mapped[list["NbaOdds"]] = relationship(back_populates="game", cascade="all, delete-orphan")  # noqa: E501
    features: Mapped["NbaFeatures | None"] = relationship(back_populates="game", uselist=False)
    signals: Mapped[list["NbaSignal"]] = relationship(back_populates="game")

    __table_args__ = (
        UniqueConstraint("home_team", "away_team", "game_date", name="uq_nba_game_matchup"),
        Index("ix_nba_games_season_date", "season", "game_date"),
        Index("ix_nba_games_status_date", "status", "game_date"),
    )


class NbaOdds(Base):
    __tablename__ = "nba_odds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nba_games.id", ondelete="CASCADE"), index=True
    )
    bookmaker: Mapped[str] = mapped_column(String(80), default="market", index=True)
    market_type: Mapped[MarketType] = mapped_column(Enum(MarketType), index=True)
    selection: Mapped[str] = mapped_column(String(160))
    line: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    odd: Mapped[float] = mapped_column(Numeric(8, 4))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())  # noqa: E501

    game: Mapped[NbaGame] = relationship(back_populates="odds")

    __table_args__ = (
        UniqueConstraint(
            "game_id", "bookmaker", "market_type", "selection", name="uq_nba_odds_market"
        ),
        Index("ix_nba_odds_game_market", "game_id", "market_type"),
    )


class NbaFeatures(Base):
    __tablename__ = "nba_features"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nba_games.id", ondelete="CASCADE"), unique=True, index=True
    )
    home_rest_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_rest_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_back_to_back: Mapped[bool] = mapped_column(Boolean, default=False)
    away_back_to_back: Mapped[bool] = mapped_column(Boolean, default=False)
    home_last5_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_last5_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_last5_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_last5_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_last10_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_last10_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_last10_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_last10_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_off_rtg: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_off_rtg: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_def_rtg: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_def_rtg: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_pace: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_pace: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())  # noqa: E501

    game: Mapped[NbaGame] = relationship(back_populates="features")


class NbaSignal(Base):
    __tablename__ = "nba_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nba_games.id", ondelete="CASCADE"), index=True
    )
    setup_name: Mapped[str] = mapped_column(String(80), index=True)
    market_type: Mapped[MarketType] = mapped_column(Enum(MarketType))
    selection: Mapped[str] = mapped_column(String(160))
    line: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    odd: Mapped[float] = mapped_column(Numeric(8, 4))
    signal_direction: Mapped[SignalDirection] = mapped_column(Enum(SignalDirection))
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)  # noqa: E501
    telegram_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    game: Mapped[NbaGame] = relationship(back_populates="signals")
    quant_bet: Mapped["NbaQuantBet | None"] = relationship(back_populates="signal", uselist=False)

    __table_args__ = (
        UniqueConstraint("game_id", "setup_name", name="uq_nba_signal_game_setup"),
        Index("ix_nba_signals_setup_created", "setup_name", "created_at"),
    )


class NbaQuantBet(Base):
    __tablename__ = "nba_quant_bets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nba_signals.id", ondelete="CASCADE"), unique=True, index=True  # noqa: E501
    )
    stake: Mapped[float] = mapped_column(Numeric(10, 4), default=1.0)
    status: Mapped[BetStatus] = mapped_column(Enum(BetStatus), default=BetStatus.pending, index=True)  # noqa: E501
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    source_bookmaker: Mapped[str] = mapped_column(String(80), default="market")
    settlement_telegram_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # noqa: E501
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    signal: Mapped[NbaSignal] = relationship(back_populates="quant_bet")


class NbaEdgeRegistry(Base):
    __tablename__ = "nba_edge_registry"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    setup_name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    total_bets: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    pending: Mapped[int] = mapped_column(Integer, default=0)
    void: Mapped[int] = mapped_column(Integer, default=0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    yield_pct: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    expectancy: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    classification: Mapped[EdgeClassification] = mapped_column(
        Enum(EdgeClassification), default=EdgeClassification.neutral, index=True
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

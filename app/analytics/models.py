import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class ProductPriceAnalytics(Base):
    __tablename__ = "product_price_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("normalized_products.id"), index=True)
    avg_price_7d: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    avg_price_30d: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    min_price_90d: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    max_price_90d: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    price_score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source_normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)


class RealEstateAnalytics(Base):
    __tablename__ = "real_estate_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("normalized_real_estate_listings.id"), index=True
    )
    price_per_m2: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    neighborhood_avg_price_m2: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    discount_vs_neighborhood: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    opportunity_score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source_normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)


class CryptoAnalytics(Base):
    __tablename__ = "crypto_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    volatility_24h: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    volume_spike_score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    trend_score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source_normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)


class TradingAnalytics(Base):
    __tablename__ = "trading_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_candle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("normalized_market_candles.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    timeframe: Mapped[str] = mapped_column(String(20), index=True)
    rsi: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    moving_average_fast: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    moving_average_slow: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    atr: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    adx: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    breakout_score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    trend_score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    signal: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    confidence: Mapped[int | None] = mapped_column(nullable=True)
    regime: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source_normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)


class SportsOddsAnalytics(Base):
    __tablename__ = "sports_odds_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    market_type: Mapped[str] = mapped_column(String(80), index=True)
    selection: Mapped[str] = mapped_column(String(160), index=True)
    opening_odd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    current_odd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    closing_odd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    line_movement: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    clv: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    ev_estimate: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source_normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

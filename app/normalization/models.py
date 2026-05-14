import uuid
from datetime import datetime

from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class NormalizedProduct(Base):
    __tablename__ = "normalized_products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("raw_collections.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    canonical_product_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    availability: Mapped[str | None] = mapped_column(String(80), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    shipping_price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    analytics_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    normalization_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_raw_schema_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_raw_schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_collector_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_collector_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    __table_args__ = (
        Index("ix_normalized_products_canonical_collected", "canonical_product_id", "collected_at"),
        Index("ix_normalized_products_source_id_collected", "source_id", "collected_at"),
        Index("ix_normalized_products_store_name_collected", "store_name", "collected_at"),
        Index("ix_normalized_products_analytics_status_collected", "analytics_status", "collected_at"),
    )


class NormalizedRealEstateListing(Base):
    __tablename__ = "normalized_real_estate_listings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("raw_collections.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    property_type: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    purpose: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    neighborhood: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    area_m2: Mapped[int | None] = mapped_column(nullable=True)
    bedrooms: Mapped[int | None] = mapped_column(nullable=True)
    bathrooms: Mapped[int | None] = mapped_column(nullable=True)
    parking_spaces: Mapped[int | None] = mapped_column(nullable=True)
    condo_fee: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    iptu: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    analytics_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    normalization_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_raw_schema_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_raw_schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_collector_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_collector_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    __table_args__ = (Index("ix_norm_real_estate_city_neighborhood", "city", "neighborhood"),)


class NormalizedCryptoSnapshot(Base):
    __tablename__ = "normalized_crypto_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("raw_collections.id"), index=True)
    source: Mapped[str] = mapped_column(String(160), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    price: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    volume: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Numeric(24, 2), nullable=True)
    change_24h: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    analytics_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    normalization_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_raw_schema_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_raw_schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_collector_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_collector_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)


class NormalizedMarketCandle(Base):
    __tablename__ = "normalized_market_candles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("raw_collections.id"), index=True)
    source: Mapped[str] = mapped_column(String(160), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    timeframe: Mapped[str] = mapped_column(String(20), index=True)
    open: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    low: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    close: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    volume: Mapped[float | None] = mapped_column(Numeric(24, 8), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    analytics_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    normalization_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_raw_schema_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_raw_schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_collector_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_collector_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    __table_args__ = (Index("ix_norm_market_candle_identity", "source", "symbol", "timeframe", "timestamp"),)


class NormalizedSportsOdd(Base):
    __tablename__ = "normalized_sports_odds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("raw_collections.id"), index=True)
    sportsbook: Mapped[str] = mapped_column(String(160), index=True)
    sport: Mapped[str] = mapped_column(String(80), index=True)
    league: Mapped[str] = mapped_column(String(120), index=True)
    event_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    home_team: Mapped[str] = mapped_column(String(160), index=True)
    away_team: Mapped[str] = mapped_column(String(160), index=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    market_type: Mapped[str] = mapped_column(String(80), index=True)
    selection: Mapped[str] = mapped_column(String(160), index=True)
    handicap: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    odd: Mapped[float] = mapped_column(Numeric(10, 4))
    implied_probability: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    analytics_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    normalizer_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    normalization_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_raw_schema_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_raw_schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_collector_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_collector_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    __table_args__ = (
        Index("ix_norm_sports_event_market", "event_external_id", "market_type", "selection"),
    )


class NormalizerVersion(Base):
    __tablename__ = "normalizer_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module: Mapped[str] = mapped_column(String(80), index=True)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    raw_schema_name: Mapped[str] = mapped_column(String(160), index=True)
    raw_schema_version: Mapped[str] = mapped_column(String(40), index=True)
    normalizer_name: Mapped[str] = mapped_column(String(160), index=True)
    normalizer_version: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "module",
            "source_name",
            "raw_schema_name",
            "raw_schema_version",
            "normalizer_name",
            "normalizer_version",
            name="uq_normalizer_version_identity",
        ),
    )

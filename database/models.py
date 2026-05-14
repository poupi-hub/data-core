import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class CollectorDomain(str, enum.Enum):
    real_estate = "real_estate"
    ecommerce = "ecommerce"
    crypto = "crypto"
    sports_betting = "sports_betting"


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    partial = "partial"


class CollectorDefinition(Base):
    __tablename__ = "collector_definitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    domain: Mapped[CollectorDomain] = mapped_column(Enum(CollectorDomain), index=True)
    source: Mapped[str] = mapped_column(String(160), index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list["CollectionRun"]] = relationship(back_populates="collector")


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    collector_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collector_definitions.id"), nullable=True
    )
    collector_name: Mapped[str] = mapped_column(String(160), index=True)
    collector_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    raw_schema_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    raw_schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    module: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    domain: Mapped[CollectorDomain | None] = mapped_column(Enum(CollectorDomain), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.pending, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_collected: Mapped[int] = mapped_column(Integer, default=0)
    raw_saved_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    collector: Mapped[CollectorDefinition | None] = relationship(back_populates="runs")
    records: Mapped[list["CollectedRecord"]] = relationship(back_populates="run")
    errors: Mapped[list["CollectorError"]] = relationship(back_populates="run")

    __table_args__ = (
        Index("ix_collection_runs_collector_started", "collector_name", "started_at"),
        Index("ix_collection_runs_schema", "module", "raw_schema_name", "raw_schema_version"),
        Index("ix_collection_runs_status_started", "status", "started_at"),
    )


class CollectedRecord(Base):
    __tablename__ = "collected_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection_runs.id"), nullable=True
    )
    collector_name: Mapped[str] = mapped_column(String(160), index=True)
    domain: Mapped[CollectorDomain] = mapped_column(Enum(CollectorDomain), index=True)
    source: Mapped[str] = mapped_column(String(160), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[CollectionRun | None] = relationship(back_populates="records")

    __table_args__ = (
        UniqueConstraint(
            "collector_name",
            "source",
            "external_id",
            "payload_hash",
            name="uq_record_identity_snapshot",
        ),
        Index("ix_collected_records_domain_collected", "domain", "collected_at"),
    )


class CollectorError(Base):
    __tablename__ = "collector_errors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection_runs.id"), nullable=True
    )
    collector_name: Mapped[str] = mapped_column(String(160), index=True)
    error_type: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[CollectionRun | None] = relationship(back_populates="errors")


class CollectionTarget(Base):
    __tablename__ = "collection_targets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module: Mapped[str] = mapped_column(String(80), index=True)
    source_name: Mapped[str] = mapped_column(String(160), index=True)
    collector_name: Mapped[str] = mapped_column(String(160), index=True)
    target_url: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("module", "source_name", "collector_name", "target_url", name="uq_collection_target_identity"),
        Index("ix_collection_targets_active_module", "active", "module"),
    )

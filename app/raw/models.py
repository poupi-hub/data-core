import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


class RawCollection(Base):
    __tablename__ = "raw_collections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module: Mapped[str] = mapped_column(String(80), index=True)
    source_name: Mapped[str] = mapped_column(String(160), index=True)
    source_type: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    collector_name: Mapped[str] = mapped_column(String(160), index=True)
    collector_version: Mapped[str] = mapped_column(String(40), default="1.0.0", index=True)
    raw_schema_name: Mapped[str] = mapped_column(String(160), default="generic", index=True)
    raw_schema_version: Mapped[str] = mapped_column(String(40), default="1.0.0", index=True)
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    request_params_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    request_headers_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    response_headers_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    processing_status: Mapped[str] = mapped_column(String(40), default="normalization_pending", index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    collection_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("module", "source_name", "checksum", name="uq_raw_collection_identity"),
        Index("ix_raw_collections_module_status", "module", "processing_status"),
        Index("ix_raw_collections_schema", "module", "raw_schema_name", "raw_schema_version"),
        Index("ix_raw_collections_source_collected", "source_name", "collected_at"),
        Index("ix_raw_collections_status_collected", "processing_status", "collected_at"),
    )


class CollectorVersion(Base):
    __tablename__ = "collector_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module: Mapped[str] = mapped_column(String(80), index=True)
    source_name: Mapped[str] = mapped_column(String(160), index=True)
    collector_name: Mapped[str] = mapped_column(String(160), index=True)
    collector_version: Mapped[str] = mapped_column(String(40), index=True)
    raw_schema_name: Mapped[str] = mapped_column(String(160), index=True)
    raw_schema_version: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "module",
            "source_name",
            "collector_name",
            "collector_version",
            "raw_schema_name",
            "raw_schema_version",
            name="uq_collector_version_identity",
        ),
    )

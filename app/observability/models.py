"""
Observability models — FASE 3, 4, 5.

Tabelas:
  source_health          — saúde operacional por coletor (FASE 3)
  dataset_integrity_scores — pontuações de qualidade por dataset (FASE 4)
  dataset_snapshots      — histórico longitudinal diário (FASE 5)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models import Base


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3 — Source Health
# ─────────────────────────────────────────────────────────────────────────────

class SourceHealth(Base):
    """Saúde operacional calculada por coletor.

    Gerada pelo scheduler job `compute_source_health_job` a cada 4h.
    Uma linha por (collector_name, computed_at) — sem upsert, mantém histórico.
    """
    __tablename__ = "source_health"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    collector_name: Mapped[str] = mapped_column(String(160), index=True)
    category: Mapped[str] = mapped_column(String(80), index=True)   # 'jobs' | 'real_estate' | ...
    source: Mapped[str] = mapped_column(String(160))                # matches metadata.source

    # Timestamps de última execução
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Métricas de confiabilidade
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    successful_runs: Mapped[int] = mapped_column(Integer, default=0)
    failed_runs: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0.0–1.0

    # Métricas de volume
    records_collected: Mapped[int] = mapped_column(Integer, default=0)      # total histórico
    records_last_run: Mapped[int] = mapped_column(Integer, default=0)       # último run
    growth_rate: Mapped[float | None] = mapped_column(Float, nullable=True) # registros/dia (7d)

    # Métricas de qualidade de dados
    duplicate_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0.0–1.0

    # Score composto e status
    health_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0–100
    status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    # HEALTHY | WARNING | DEGRADED | CRITICAL | BLOCKED | UNKNOWN

    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_source_health_collector_time", "collector_name", "computed_at"),
        Index("ix_source_health_status", "status", "computed_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# FASE 4 — Dataset Integrity Scores
# ─────────────────────────────────────────────────────────────────────────────

class DatasetIntegrityScore(Base):
    """Pontuações de integridade e qualidade por dataset.

    Calculada pelo scheduler `compute_dataset_integrity_job` a cada 6h.
    Uma linha por (dataset, source, computed_at).
    source=NULL → score agregado de todo o dataset.
    """
    __tablename__ = "dataset_integrity_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    dataset: Mapped[str] = mapped_column(String(80), index=True)    # 'jobs' | 'real_estate'
    source: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)

    # Scores individuais (0–100)
    freshness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    completeness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    consistency_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    duplication_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Score final combinado (0–100)
    dataset_health_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Contagens usadas no cálculo
    total_records: Mapped[int] = mapped_column(Integer, default=0)
    records_with_title: Mapped[int] = mapped_column(Integer, default=0)
    records_with_company: Mapped[int] = mapped_column(Integer, default=0)
    records_with_location: Mapped[int] = mapped_column(Integer, default=0)
    records_with_url: Mapped[int] = mapped_column(Integer, default=0)
    records_with_price: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)

    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_dataset_integrity_dataset_time", "dataset", "computed_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# FASE 5 — Longitudinal Tracking (Dataset Snapshots)
# ─────────────────────────────────────────────────────────────────────────────

class DatasetSnapshot(Base):
    """Snapshot diário do estado do dataset — base do histórico longitudinal.

    Persiste 1 linha por (snapshot_date, dataset, source) por dia.
    Permite responder: "quais fontes crescem? quais estagnaram? quais morreram?"
    """
    __tablename__ = "dataset_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    dataset: Mapped[str] = mapped_column(String(80), index=True)    # 'jobs' | 'real_estate'
    source: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # source=NULL → agregado do dataset completo

    # Volume
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    new_records_today: Mapped[int] = mapped_column(Integer, default=0)

    # Scores (copiados de DatasetIntegrityScore ou calculados na hora)
    health_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    freshness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    completeness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    duplication_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("snapshot_date", "dataset", "source", name="uq_snapshot_date_dataset_source"),
        Index("ix_dataset_snapshots_dataset_date", "dataset", "snapshot_date"),
    )

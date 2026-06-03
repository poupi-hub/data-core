"""0022_observability_tables — cria tabelas de observabilidade de dados.

Novas tabelas:
  source_health          — saúde operacional por coletor (FASE 3)
  dataset_integrity_scores — pontuações de integridade por dataset (FASE 4)
  dataset_snapshots      — histórico longitudinal diário (FASE 5)

Compatível com PostgreSQL 13+. Sem dependências de migrações anteriores
exceto a existência das tabelas base.
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0022_observability_tables"
down_revision: str | None = "0021_jobs_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── source_health ────────────────────────────────────────────────────────
    op.create_table(
        "source_health",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("collector_name", sa.String(160), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("source", sa.String(160), nullable=False),
        sa.Column("last_success", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_runs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("successful_runs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_runs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("success_rate", sa.Float, nullable=True),
        sa.Column("records_collected", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_last_run", sa.Integer, nullable=False, server_default="0"),
        sa.Column("growth_rate", sa.Float, nullable=True),
        sa.Column("duplicate_rate", sa.Float, nullable=True),
        sa.Column("health_score", sa.Float, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'UNKNOWN'")),
        sa.Column("details_json", pg.JSONB, nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_source_health_collector_time", "source_health", ["collector_name", "computed_at"])
    op.create_index("ix_source_health_status", "source_health", ["status", "computed_at"])
    op.create_index("ix_source_health_computed_at", "source_health", ["computed_at"])

    # ── dataset_integrity_scores ─────────────────────────────────────────────
    op.create_table(
        "dataset_integrity_scores",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("source", sa.String(160), nullable=True),
        sa.Column("freshness_score", sa.Float, nullable=True),
        sa.Column("completeness_score", sa.Float, nullable=True),
        sa.Column("consistency_score", sa.Float, nullable=True),
        sa.Column("duplication_score", sa.Float, nullable=True),
        sa.Column("coverage_score", sa.Float, nullable=True),
        sa.Column("dataset_health_score", sa.Float, nullable=True),
        sa.Column("total_records", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_with_title", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_with_company", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_with_location", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_with_url", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_with_price", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("details_json", pg.JSONB, nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_dataset_integrity_dataset_time", "dataset_integrity_scores", ["dataset", "computed_at"])
    op.create_index("ix_dataset_integrity_computed_at", "dataset_integrity_scores", ["computed_at"])

    # ── dataset_snapshots ────────────────────────────────────────────────────
    op.create_table(
        "dataset_snapshots",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("source", sa.String(160), nullable=True),
        sa.Column("record_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("new_records_today", sa.Integer, nullable=False, server_default="0"),
        sa.Column("health_score", sa.Float, nullable=True),
        sa.Column("freshness_score", sa.Float, nullable=True),
        sa.Column("completeness_score", sa.Float, nullable=True),
        sa.Column("duplication_score", sa.Float, nullable=True),
        sa.Column("coverage_score", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("details_json", pg.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.UniqueConstraint("snapshot_date", "dataset", "source", name="uq_snapshot_date_dataset_source"),
    )
    op.create_index("ix_dataset_snapshots_dataset_date", "dataset_snapshots", ["dataset", "snapshot_date"])
    op.create_index("ix_dataset_snapshots_date", "dataset_snapshots", ["snapshot_date"])


def downgrade() -> None:
    op.drop_table("dataset_snapshots")
    op.drop_table("dataset_integrity_scores")
    op.drop_table("source_health")

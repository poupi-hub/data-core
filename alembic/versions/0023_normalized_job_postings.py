"""0023_normalized_job_postings — cria tabela normalized_job_postings.

Normaliza vagas coletadas pelos collectors jobs.* (recruitee, smartrecruiters,
teamtailor, greenhouse, lever, workable) a partir de raw_collections (module=jobs).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0023_normalized_job_postings"
down_revision: str | None = "0022_observability_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "normalized_job_postings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("raw_collection_id", UUID(as_uuid=True), sa.ForeignKey("raw_collections.id"), nullable=False, index=True),
        sa.Column("external_id", sa.String(255), nullable=True, index=True),
        sa.Column("source", sa.String(80), nullable=False, index=True),
        sa.Column("company_id", sa.String(255), nullable=True, index=True),
        sa.Column("company_name", sa.String(255), nullable=True, index=True),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("department", sa.String(255), nullable=True, index=True),
        sa.Column("city", sa.String(120), nullable=True, index=True),
        sa.Column("country", sa.String(80), nullable=True, index=True),
        sa.Column("remote", sa.Boolean, nullable=True),
        sa.Column("employment_type", sa.String(80), nullable=True, index=True),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("analytics_status", sa.String(40), nullable=False, server_default="pending", index=True),
        sa.Column("normalizer_name", sa.String(160), nullable=True, index=True),
        sa.Column("normalizer_version", sa.String(40), nullable=True, index=True),
        sa.Column("normalized_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("normalization_metadata_json", JSONB, nullable=False, server_default="{}"),
        sa.Column("source_raw_schema_name", sa.String(160), nullable=True, index=True),
        sa.Column("source_raw_schema_version", sa.String(40), nullable=True, index=True),
        sa.Column("source_collector_name", sa.String(160), nullable=True, index=True),
        sa.Column("source_collector_version", sa.String(40), nullable=True, index=True),
    )
    op.create_index("ix_norm_job_source_company_collected", "normalized_job_postings", ["source", "company_id", "collected_at"])
    op.create_index("ix_norm_job_title_country_collected", "normalized_job_postings", ["title", "country", "collected_at"])


def downgrade() -> None:
    op.drop_table("normalized_job_postings")

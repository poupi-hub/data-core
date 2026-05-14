"""Add composite indexes for stale-run queries and target lock checks.

Revision ID: 0010_collection_run_indexes
Revises: 0009_collection_targets
Create Date: 2026-05-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_collection_run_indexes"
down_revision: str | None = "0009_collection_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Used by cleanup_stale_runs_job and _has_running_target_lock
    op.create_index(
        "ix_collection_runs_status_started",
        "collection_runs",
        ["status", "started_at"],
    )
    # Used by operations/freshness and operations/alerts ordering
    op.create_index(
        "ix_raw_collections_status_collected",
        "raw_collections",
        ["processing_status", "collected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_raw_collections_status_collected", table_name="raw_collections")
    op.drop_index("ix_collection_runs_status_started", table_name="collection_runs")

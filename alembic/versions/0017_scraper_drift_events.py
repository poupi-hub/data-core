"""0017_scraper_drift_events — scraper structural drift event log.

Creates the scraper_drift_events table used by StructuralDriftDetector
to persist detected payload schema changes per source / collector.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_scraper_drift"
down_revision: str | None = "0016_prod_source_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scraper_drift_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(128), nullable=False, index=True),
        sa.Column("collector_name", sa.String(128), nullable=False),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column(
            "drift_type",
            sa.String(64),
            nullable=False,
            comment="field_missing | field_added | type_changed | price_zero | availability_unknown | strategy_fallback",
        ),
        sa.Column(
            "risk_level",
            sa.String(16),
            nullable=False,
            comment="low | medium | high | critical",
        ),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("field_name", sa.String(128), nullable=True),
        sa.Column("prev_signature", sa.JSON, nullable=True),
        sa.Column("curr_signature", sa.JSON, nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
            index=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("scraper_drift_events")

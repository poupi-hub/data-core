"""signal edge outcomes tracking

Revision ID: 0028_signal_edge_outcomes
Revises: 0027_merge_jobs_and_incidents
Create Date: 2026-06-05

NOTE: This migration file was recreated to match a migration that was applied
to production but whose file was accidentally removed from the repository.
The trading_edge_outcomes table already exists in production.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_signal_edge_outcomes"
down_revision = "0027_merge_jobs_and_incidents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trading_edge_outcomes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            analytics_id UUID,
            horizon_hours INTEGER,
            symbol VARCHAR(40),
            timeframe VARCHAR(20),
            signal VARCHAR(40),
            confidence INTEGER,
            regime VARCHAR(80),
            signal_at TIMESTAMP,
            signal_price NUMERIC(24, 8),
            outcome_at TIMESTAMP,
            outcome_price NUMERIC(24, 8),
            candles_elapsed INTEGER,
            price_change_pct NUMERIC(10, 4),
            mfe_pct NUMERIC(10, 4),
            mae_pct NUMERIC(10, 4),
            outcome_correct BOOLEAN,
            computed_at TIMESTAMP
        )
        """
    )


def downgrade() -> None:
    op.drop_table("trading_edge_outcomes")

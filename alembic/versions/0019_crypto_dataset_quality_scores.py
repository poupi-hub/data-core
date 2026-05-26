"""0019_crypto_dataset_quality_scores — per-symbol/timeframe candle dataset quality scores.

Creates:
  - crypto_dataset_quality_scores : freshness, coverage, OHLC integrity scores per symbol/timeframe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_crypto_dataset_quality_scores"
down_revision: str | None = "0018_watchdog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_dataset_quality_scores",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(40), nullable=False, index=True),
        sa.Column("timeframe", sa.String(20), nullable=False, index=True),
        sa.Column("integrity_score", sa.Numeric(6, 2), nullable=True, comment="0-100 composite score"),
        sa.Column("freshness_score", sa.Numeric(6, 2), nullable=True, comment="0-40 pts"),
        sa.Column("coverage_score", sa.Numeric(6, 2), nullable=True, comment="0-40 pts"),
        sa.Column("ohlc_score", sa.Numeric(6, 2), nullable=True, comment="0-20 pts"),
        sa.Column("staleness_hours", sa.Numeric(8, 2), nullable=True, comment="Hours since last candle"),
        sa.Column("coverage_pct", sa.Numeric(6, 2), nullable=True, comment="% of expected candles in 24h"),
        sa.Column("gap_count", sa.Integer, nullable=True, comment="Missing intervals in last 24h"),
        sa.Column("total_candles_24h", sa.Integer, nullable=True),
        sa.Column("expected_candles_24h", sa.Integer, nullable=True),
        sa.Column("components_json", sa.JSON, nullable=True),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
            index=True,
        ),
    )
    op.create_index(
        "ix_crypto_dq_symbol_timeframe",
        "crypto_dataset_quality_scores",
        ["symbol", "timeframe"],
    )


def downgrade() -> None:
    op.drop_index("ix_crypto_dq_symbol_timeframe", table_name="crypto_dataset_quality_scores")
    op.drop_table("crypto_dataset_quality_scores")

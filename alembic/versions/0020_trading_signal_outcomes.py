"""0020_trading_signal_outcomes — retrospective outcome evaluation for BUY/SELL signals.

Creates:
  - trading_signal_outcomes : price movement, MFE/MAE, outcome_correct per signal
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020_trading_signal_outcomes"
down_revision: str | None = "0019_crypto_dataset_quality_scores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trading_signal_outcomes",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # FK to trading_analytics — nullable to allow orphan outcomes if analytics row deleted
        sa.Column(
            "analytics_id",
            sa.UUID(),
            sa.ForeignKey("trading_analytics.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
            index=True,
            comment="FK → trading_analytics.id (unique: one outcome per signal)",
        ),
        sa.Column("symbol", sa.String(40), nullable=False, index=True),
        sa.Column("timeframe", sa.String(20), nullable=False),
        sa.Column("signal", sa.String(40), nullable=False, index=True, comment="BUY | SELL"),
        sa.Column("confidence", sa.Integer, nullable=True),
        sa.Column("regime", sa.String(80), nullable=True),
        sa.Column("signal_price", sa.Numeric(24, 8), nullable=True, comment="Close price at signal candle"),
        sa.Column(
            "signal_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="Timestamp of signal candle",
        ),
        sa.Column("outcome_price", sa.Numeric(24, 8), nullable=True, comment="Close at evaluation horizon"),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("candles_elapsed", sa.Integer, nullable=True),
        sa.Column(
            "price_change_pct",
            sa.Numeric(10, 4),
            nullable=True,
            comment="(outcome_price - signal_price) / signal_price * 100",
        ),
        sa.Column("max_favorable_pct", sa.Numeric(10, 4), nullable=True, comment="MFE: max favorable excursion"),
        sa.Column("max_adverse_pct", sa.Numeric(10, 4), nullable=True, comment="MAE: max adverse excursion"),
        sa.Column(
            "outcome_correct",
            sa.Boolean,
            nullable=True,
            index=True,
            comment="BUY && price_change>0, SELL && price_change<0",
        ),
        sa.Column(
            "evaluation_horizon_candles",
            sa.Integer,
            nullable=False,
            server_default="6",
            comment="How many candles ahead was outcome evaluated",
        ),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_signal_outcomes_symbol_tf_at",
        "trading_signal_outcomes",
        ["symbol", "timeframe", "signal_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_signal_outcomes_symbol_tf_at", table_name="trading_signal_outcomes")
    op.drop_table("trading_signal_outcomes")

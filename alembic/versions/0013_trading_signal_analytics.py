"""Add trading signal analytics fields.

Revision ID: 0013_trading_signal_analytics
Revises: 0012_canonical_backfill
Create Date: 2026-05-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_trading_signal_analytics"
down_revision: str | None = "0012_canonical_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trading_analytics", sa.Column("market_candle_id", sa.UUID(), nullable=True))
    op.add_column("trading_analytics", sa.Column("adx", sa.Numeric(10, 4), nullable=True))
    op.add_column("trading_analytics", sa.Column("volume_ratio", sa.Numeric(10, 4), nullable=True))
    op.add_column("trading_analytics", sa.Column("breakout_score", sa.Numeric(10, 4), nullable=True))
    op.add_column("trading_analytics", sa.Column("signal", sa.String(length=40), nullable=True))
    op.add_column("trading_analytics", sa.Column("confidence", sa.Integer(), nullable=True))
    op.add_column("trading_analytics", sa.Column("regime", sa.String(length=80), nullable=True))
    op.create_foreign_key(
        "fk_trading_analytics_market_candle_id",
        "trading_analytics",
        "normalized_market_candles",
        ["market_candle_id"],
        ["id"],
    )
    op.create_index(op.f("ix_trading_analytics_market_candle_id"), "trading_analytics", ["market_candle_id"], unique=False)
    op.create_index(op.f("ix_trading_analytics_signal"), "trading_analytics", ["signal"], unique=False)
    op.create_index(op.f("ix_trading_analytics_regime"), "trading_analytics", ["regime"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_trading_analytics_regime"), table_name="trading_analytics")
    op.drop_index(op.f("ix_trading_analytics_signal"), table_name="trading_analytics")
    op.drop_index(op.f("ix_trading_analytics_market_candle_id"), table_name="trading_analytics")
    op.drop_constraint("fk_trading_analytics_market_candle_id", "trading_analytics", type_="foreignkey")
    op.drop_column("trading_analytics", "regime")
    op.drop_column("trading_analytics", "confidence")
    op.drop_column("trading_analytics", "signal")
    op.drop_column("trading_analytics", "breakout_score")
    op.drop_column("trading_analytics", "volume_ratio")
    op.drop_column("trading_analytics", "adx")
    op.drop_column("trading_analytics", "market_candle_id")

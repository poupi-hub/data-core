"""Add unique constraint on normalized_market_candles identity.

Revision ID: 0014_unique_market_candle_identity
Revises: 0013_trading_signal_analytics
Create Date: 2026-05-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_unique_market_candle_identity"
down_revision: str | None = "0013_trading_signal_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Remove o índice não-único existente
    op.drop_index("ix_norm_market_candle_identity", table_name="normalized_market_candles")

    # Cria constraint única no lugar
    op.create_unique_constraint(
        "uq_norm_market_candle_identity",
        "normalized_market_candles",
        ["source", "symbol", "timeframe", "timestamp"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_norm_market_candle_identity", "normalized_market_candles")
    op.create_index(
        "ix_norm_market_candle_identity",
        "normalized_market_candles",
        ["source", "symbol", "timeframe", "timestamp"],
    )

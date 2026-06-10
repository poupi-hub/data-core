"""nba_telegram_dedup_betfair

Adds telegram_sent_at to nba_signals (dedup) and source_bookmaker to nba_quant_bets.

Revision ID: a1b2c3d4e5f6
Revises: 74f248e42006
Create Date: 2026-06-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "74f248e42006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nba_signals",
        sa.Column("telegram_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "nba_quant_bets",
        sa.Column(
            "source_bookmaker",
            sa.String(length=80),
            nullable=False,
            server_default="market",
        ),
    )
    op.add_column(
        "nba_quant_bets",
        sa.Column("settlement_telegram_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("nba_quant_bets", "settlement_telegram_sent_at")
    op.drop_column("nba_quant_bets", "source_bookmaker")
    op.drop_column("nba_signals", "telegram_sent_at")

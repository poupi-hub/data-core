"""forward shadow signal tracking — Phase 8

Revision ID: 0029_forward_shadow_signals
Revises: 74f248e42006
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_forward_shadow_signals"
down_revision = "74f248e42006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forward_shadow_signals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("analytics_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("timeframe", sa.String(length=10), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("regime", sa.String(length=50), nullable=True),
        sa.Column("signal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signal_price", sa.Numeric(precision=20, scale=8), nullable=True),
        # 24-hour horizon
        sa.Column("return_24h", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("outcome_correct_24h", sa.Boolean(), nullable=True),
        sa.Column("outcome_at_24h", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfe_24h", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("mae_24h", sa.Numeric(precision=10, scale=4), nullable=True),
        # 72-hour horizon
        sa.Column("return_72h", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("outcome_correct_72h", sa.Boolean(), nullable=True),
        sa.Column("outcome_at_72h", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfe_72h", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("mae_72h", sa.Numeric(precision=10, scale=4), nullable=True),
        # 168-hour horizon
        sa.Column("return_168h", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("outcome_correct_168h", sa.Boolean(), nullable=True),
        sa.Column("outcome_at_168h", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfe_168h", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("mae_168h", sa.Numeric(precision=10, scale=4), nullable=True),
        # Alert tracking flags
        sa.Column(
            "alert_entry_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "alert_24h_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "alert_72h_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "alert_168h_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["analytics_id"],
            ["trading_analytics.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analytics_id", name="uq_forward_shadow_analytics"),
    )
    op.create_index(
        "ix_forward_shadow_signal_at",
        "forward_shadow_signals",
        ["signal_at"],
    )
    op.create_index(
        "ix_forward_shadow_symbol_tf",
        "forward_shadow_signals",
        ["symbol", "timeframe"],
    )


def downgrade() -> None:
    op.drop_index("ix_forward_shadow_symbol_tf", table_name="forward_shadow_signals")
    op.drop_index("ix_forward_shadow_signal_at", table_name="forward_shadow_signals")
    op.drop_table("forward_shadow_signals")

"""add_nba_picks_pipeline

Revision ID: 34735fc7e2a2
Revises: 0027_merge_jobs_and_incidents
Create Date: 2026-06-07 19:52:30.360026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "34735fc7e2a2"
down_revision: str | None = "0027_merge_jobs_and_incidents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nba_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("event_description", sa.String(length=255), nullable=False),
        sa.Column("home_team", sa.String(length=160), nullable=False),
        sa.Column("away_team", sa.String(length=160), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("total_points", sa.Numeric(precision=8, scale=1), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("home_team", "away_team", "event_time", name="uq_nba_result_matchup"),
    )
    op.create_index(op.f("ix_nba_results_away_team"), "nba_results", ["away_team"], unique=False)
    op.create_index(op.f("ix_nba_results_event_description"), "nba_results", ["event_description"], unique=False)
    op.create_index(op.f("ix_nba_results_event_time"), "nba_results", ["event_time"], unique=False)
    op.create_index(op.f("ix_nba_results_home_team"), "nba_results", ["home_team"], unique=False)

    op.create_table(
        "nba_sources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("source_type", sa.Enum("telegram", "discord", "x", "manual", name="sourcetype"), nullable=False),
        sa.Column("handle", sa.String(length=160), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_nba_sources_active"), "nba_sources", ["active"], unique=False)
    op.create_index(op.f("ix_nba_sources_name"), "nba_sources", ["name"], unique=True)
    op.create_index(op.f("ix_nba_sources_source_type"), "nba_sources", ["source_type"], unique=False)

    op.create_table(
        "nba_picks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column(
            "pick_type",
            sa.Enum("moneyline", "spread", "total", "player_prop", name="picktype"),
            nullable=False,
        ),
        sa.Column("team", sa.String(length=160), nullable=True),
        sa.Column("player", sa.String(length=160), nullable=True),
        sa.Column("line", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("odd", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("event_description", sa.String(length=255), nullable=True),
        sa.Column("league", sa.String(length=80), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("parse_status", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["nba_sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_nba_picks_captured_at"), "nba_picks", ["captured_at"], unique=False)
    op.create_index(op.f("ix_nba_picks_league"), "nba_picks", ["league"], unique=False)
    op.create_index(op.f("ix_nba_picks_parse_status"), "nba_picks", ["parse_status"], unique=False)
    op.create_index(op.f("ix_nba_picks_pick_type"), "nba_picks", ["pick_type"], unique=False)
    op.create_index("ix_nba_picks_source_captured", "nba_picks", ["source_id", "captured_at"], unique=False)
    op.create_index(op.f("ix_nba_picks_source_id"), "nba_picks", ["source_id"], unique=False)
    op.create_index(op.f("ix_nba_picks_team"), "nba_picks", ["team"], unique=False)

    op.create_table(
        "nba_paper_bets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("pick_id", sa.UUID(), nullable=False),
        sa.Column("stake", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("status", sa.Enum("pending", "won", "lost", "void", name="pickstatus"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pnl", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["pick_id"], ["nba_picks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_nba_paper_bets_pick_id"), "nba_paper_bets", ["pick_id"], unique=True)
    op.create_index(op.f("ix_nba_paper_bets_status"), "nba_paper_bets", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_nba_paper_bets_status"), table_name="nba_paper_bets")
    op.drop_index(op.f("ix_nba_paper_bets_pick_id"), table_name="nba_paper_bets")
    op.drop_table("nba_paper_bets")
    op.drop_index(op.f("ix_nba_picks_team"), table_name="nba_picks")
    op.drop_index(op.f("ix_nba_picks_source_id"), table_name="nba_picks")
    op.drop_index("ix_nba_picks_source_captured", table_name="nba_picks")
    op.drop_index(op.f("ix_nba_picks_pick_type"), table_name="nba_picks")
    op.drop_index(op.f("ix_nba_picks_parse_status"), table_name="nba_picks")
    op.drop_index(op.f("ix_nba_picks_league"), table_name="nba_picks")
    op.drop_index(op.f("ix_nba_picks_captured_at"), table_name="nba_picks")
    op.drop_table("nba_picks")
    op.drop_index(op.f("ix_nba_sources_source_type"), table_name="nba_sources")
    op.drop_index(op.f("ix_nba_sources_name"), table_name="nba_sources")
    op.drop_index(op.f("ix_nba_sources_active"), table_name="nba_sources")
    op.drop_table("nba_sources")
    op.drop_index(op.f("ix_nba_results_home_team"), table_name="nba_results")
    op.drop_index(op.f("ix_nba_results_event_time"), table_name="nba_results")
    op.drop_index(op.f("ix_nba_results_event_description"), table_name="nba_results")
    op.drop_index(op.f("ix_nba_results_away_team"), table_name="nba_results")
    op.drop_table("nba_results")
    op.execute("DROP TYPE IF EXISTS sourcetype")
    op.execute("DROP TYPE IF EXISTS picktype")
    op.execute("DROP TYPE IF EXISTS pickstatus")

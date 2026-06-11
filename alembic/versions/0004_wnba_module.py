"""Add WNBA quant module tables.

Revision ID: 0004_wnba_module
Revises: 0003_sports_odds_module
Create Date: 2026-06-11

NOTE: The numeric prefix "0004" is intentionally shared with
0004_flexible_raw_contract (down_revision: 39d33505c86b).
These two migrations are on SEPARATE branches of the Alembic DAG:
  branch A: ...→ 0003_sports_odds_module → 0004_wnba_module → ...
  branch B: ...→ 39d33505c86b           → 0004_flexible_raw_contract → ...
Both branches are merged downstream. There is no functional conflict;
the duplicate numeric prefix is a naming artefact, not an Alembic error.

Tables created:
  wnba_games, wnba_odds, wnba_features,
  wnba_signals, wnba_quant_bets, wnba_edge_registry

Enums reused from NBA (gamestatus, markettype, signaldirection, betstatus,
edgeclassification) — PostgreSQL CREATE TYPE IF NOT EXISTS pattern used.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_wnba_module"
down_revision: str | None = "0003_sports_odds_module"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Shared enum types — already exist from NBA migration; we reference them by name.
_gamestatus = postgresql.ENUM(
    "scheduled", "live", "final", name="gamestatus", create_type=False
)
_markettype = postgresql.ENUM(
    "moneyline", "spread", "totals", name="markettype", create_type=False
)
_signaldirection = postgresql.ENUM(
    "home", "away", "over", "under", name="signaldirection", create_type=False
)
_betstatus = postgresql.ENUM(
    "pending", "won", "lost", "void", name="betstatus", create_type=False
)
_edgeclassification = postgresql.ENUM(
    "profitable", "neutral", "losing", name="edgeclassification", create_type=False
)


def upgrade() -> None:
    # ── wnba_games ────────────────────────────────────────────────────────────
    op.create_table(
        "wnba_games",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=80), nullable=True),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("home_team", sa.String(length=160), nullable=False),
        sa.Column("away_team", sa.String(length=160), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("status", _gamestatus, nullable=False, server_default="scheduled"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("home_team", "away_team", "game_date", name="uq_wnba_game_matchup"),
    )
    op.create_index("ix_wnba_games_external_id", "wnba_games", ["external_id"])
    op.create_index("ix_wnba_games_season", "wnba_games", ["season"])
    op.create_index("ix_wnba_games_game_date", "wnba_games", ["game_date"])
    op.create_index("ix_wnba_games_home_team", "wnba_games", ["home_team"])
    op.create_index("ix_wnba_games_away_team", "wnba_games", ["away_team"])
    op.create_index("ix_wnba_games_status", "wnba_games", ["status"])
    op.create_index("ix_wnba_games_season_date", "wnba_games", ["season", "game_date"])
    op.create_index("ix_wnba_games_status_date", "wnba_games", ["status", "game_date"])

    # ── wnba_odds ─────────────────────────────────────────────────────────────
    op.create_table(
        "wnba_odds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bookmaker", sa.String(length=80), nullable=False, server_default="market"),
        sa.Column("market_type", _markettype, nullable=False),
        sa.Column("selection", sa.String(length=160), nullable=False),
        sa.Column("line", sa.Numeric(8, 2), nullable=True),
        sa.Column("odd", sa.Numeric(8, 4), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["wnba_games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "bookmaker", "market_type", "selection", name="uq_wnba_odds_market"),
    )
    op.create_index("ix_wnba_odds_game_id", "wnba_odds", ["game_id"])
    op.create_index("ix_wnba_odds_bookmaker", "wnba_odds", ["bookmaker"])
    op.create_index("ix_wnba_odds_market_type", "wnba_odds", ["market_type"])
    op.create_index("ix_wnba_odds_game_market", "wnba_odds", ["game_id", "market_type"])

    # ── wnba_features ─────────────────────────────────────────────────────────
    op.create_table(
        "wnba_features",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("home_rest_days", sa.Integer(), nullable=True),
        sa.Column("away_rest_days", sa.Integer(), nullable=True),
        sa.Column("home_back_to_back", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("away_back_to_back", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("home_last5_wins", sa.Integer(), nullable=True),
        sa.Column("home_last5_games", sa.Integer(), nullable=True),
        sa.Column("away_last5_wins", sa.Integer(), nullable=True),
        sa.Column("away_last5_games", sa.Integer(), nullable=True),
        sa.Column("home_last10_wins", sa.Integer(), nullable=True),
        sa.Column("home_last10_games", sa.Integer(), nullable=True),
        sa.Column("away_last10_wins", sa.Integer(), nullable=True),
        sa.Column("away_last10_games", sa.Integer(), nullable=True),
        sa.Column("home_off_rtg", sa.Float(), nullable=True),
        sa.Column("away_off_rtg", sa.Float(), nullable=True),
        sa.Column("home_def_rtg", sa.Float(), nullable=True),
        sa.Column("away_def_rtg", sa.Float(), nullable=True),
        sa.Column("home_pace", sa.Float(), nullable=True),
        sa.Column("away_pace", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["wnba_games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id"),
    )
    op.create_index("ix_wnba_features_game_id", "wnba_features", ["game_id"])

    # ── wnba_signals ──────────────────────────────────────────────────────────
    op.create_table(
        "wnba_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("setup_name", sa.String(length=80), nullable=False),
        sa.Column("market_type", _markettype, nullable=False),
        sa.Column("selection", sa.String(length=160), nullable=False),
        sa.Column("line", sa.Numeric(8, 2), nullable=True),
        sa.Column("odd", sa.Numeric(8, 4), nullable=False),
        sa.Column("signal_direction", _signaldirection, nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("telegram_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["wnba_games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "setup_name", name="uq_wnba_signal_game_setup"),
    )
    op.create_index("ix_wnba_signals_game_id", "wnba_signals", ["game_id"])
    op.create_index("ix_wnba_signals_setup_name", "wnba_signals", ["setup_name"])
    op.create_index("ix_wnba_signals_created_at", "wnba_signals", ["created_at"])
    op.create_index("ix_wnba_signals_setup_created", "wnba_signals", ["setup_name", "created_at"])

    # ── wnba_quant_bets ───────────────────────────────────────────────────────
    op.create_table(
        "wnba_quant_bets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stake", sa.Numeric(10, 4), nullable=False, server_default="1.0"),
        sa.Column("status", _betstatus, nullable=False, server_default="pending"),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pnl", sa.Numeric(12, 4), nullable=True),
        sa.Column("source_bookmaker", sa.String(length=80), nullable=False, server_default="market"),
        sa.Column("settlement_telegram_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["signal_id"], ["wnba_signals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signal_id"),
    )
    op.create_index("ix_wnba_quant_bets_signal_id", "wnba_quant_bets", ["signal_id"])
    op.create_index("ix_wnba_quant_bets_status", "wnba_quant_bets", ["status"])

    # ── wnba_edge_registry ────────────────────────────────────────────────────
    op.create_table(
        "wnba_edge_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("setup_name", sa.String(length=80), nullable=False),
        sa.Column("total_bets", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("void", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("roi", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("yield_pct", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("win_rate", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("profit_factor", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("expectancy", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("max_drawdown", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("classification", _edgeclassification, nullable=False, server_default="neutral"),
        sa.Column("last_updated", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setup_name"),
    )
    op.create_index("ix_wnba_edge_registry_setup_name", "wnba_edge_registry", ["setup_name"])
    op.create_index("ix_wnba_edge_registry_classification", "wnba_edge_registry", ["classification"])


def downgrade() -> None:
    op.drop_table("wnba_edge_registry")
    op.drop_table("wnba_quant_bets")
    op.drop_table("wnba_signals")
    op.drop_table("wnba_features")
    op.drop_table("wnba_odds")
    op.drop_table("wnba_games")

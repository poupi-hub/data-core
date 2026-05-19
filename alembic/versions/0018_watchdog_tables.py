"""0018_watchdog_tables — operational watchdog run log and Telegram publication events.

Creates:
  - watchdog_runs         : record of each watchdog execution with check results
  - telegram_publication_events : publication events reported by poupi-baby (callback)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018_watchdog"
down_revision: str | None = "0017_scraper_drift"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchdog_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "overall_status",
            sa.String(16),
            nullable=False,
            comment="ok | warning | critical",
        ),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        # JSON blobs: check_results list, alert_codes list, metrics snapshot
        sa.Column("check_results", sa.JSON, nullable=True),
        sa.Column("alert_codes", sa.JSON, nullable=True),    # list of alert code strings
        sa.Column("metrics_snapshot", sa.JSON, nullable=True),
        sa.Column("telegram_sent", sa.Boolean, default=False, nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
    )

    op.create_table(
        "telegram_publication_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.String(128), nullable=True),
        sa.Column("product_id", sa.String(128), nullable=True),
        sa.Column("offer_id", sa.String(128), nullable=True),
        sa.Column("marketplace", sa.String(64), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("deal_score", sa.Float, nullable=True),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            comment="sent | failed | rate_limited | skipped",
        ),
        sa.Column("fail_reason", sa.Text, nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
            index=True,
        ),
        sa.Column("reported_by", sa.String(64), nullable=True, comment="poupi-baby service name"),
    )


def downgrade() -> None:
    op.drop_table("telegram_publication_events")
    op.drop_table("watchdog_runs")

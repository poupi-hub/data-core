"""incident_events table — Alert System V2 / Incident Event Bus

Revision ID: 0024_incident_events
Revises: 39d33505c86b
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_incident_events"
down_revision = "39d33505c86b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_events",

        # ── Identity ──────────────────────────────────────────────────────────
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),

        # ── Alert metadata (from Alertmanager labels) ─────────────────────────
        sa.Column("alert_id",  sa.String(32),  nullable=True),   # INFRA-001, BUSI-006, ...
        sa.Column("alertname", sa.String(128), nullable=False),
        sa.Column("service",   sa.String(64),  nullable=True),
        sa.Column("severity",  sa.String(16),  nullable=False),
        sa.Column("category",  sa.String(32),  nullable=True),
        sa.Column("channel",   sa.String(32),  nullable=True),   # critical/operational/business/executive
        sa.Column("component", sa.String(64),  nullable=True),
        sa.Column("layer",     sa.String(64),  nullable=True),
        sa.Column("runtime",   sa.String(32),  nullable=True),

        # ── AI metadata (from alert labels, for Phase 9/10) ────────────────────
        sa.Column("ai_action", sa.String(256), nullable=True),   # check_logs,check_health,...
        sa.Column("runbook",   sa.String(256), nullable=True),   # operations/runbooks/...

        # ── Event status ──────────────────────────────────────────────────────
        sa.Column("status",    sa.String(16),  nullable=False),  # firing | resolved

        # ── Human-readable context (from annotations) ─────────────────────────
        sa.Column("summary",        sa.Text(), nullable=True),
        sa.Column("impact",         sa.Text(), nullable=True),
        sa.Column("possible_cause", sa.Text(), nullable=True),

        # ── Raw payload (full Alertmanager webhook body) ──────────────────────
        sa.Column("labels",      JSONB, nullable=True),
        sa.Column("annotations", JSONB, nullable=True),
        sa.Column("raw_payload", JSONB, nullable=True),

        # ── Timing ────────────────────────────────────────────────────────────
        sa.Column("fired_at",    sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),

        # ── RCA fields (populated by Phase 9 RCA Engine) ─────────────────────
        sa.Column("root_cause",       sa.Text(), nullable=True),
        sa.Column("rca_confidence",   sa.Float(), nullable=True),   # 0.0 – 1.0
        sa.Column("rca_hypothesis",   sa.Text(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),

        # ── Processing state ──────────────────────────────────────────────────
        sa.Column("processed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("processing_error", sa.Text(), nullable=True),
    )

    # Primary key
    op.create_index("ix_incident_events_id", "incident_events", ["id"])

    # Lookup by fingerprint (Alertmanager dedup key)
    op.create_index("ix_incident_events_fingerprint", "incident_events", ["fingerprint"])

    # Query by alert_id (canonical ID)
    op.create_index("ix_incident_events_alert_id", "incident_events", ["alert_id"])

    # Query by service + severity (most common operational queries)
    op.create_index("ix_incident_events_service_severity",
                    "incident_events", ["service", "severity"])

    # Query by status + received_at (dashboard queries)
    op.create_index("ix_incident_events_status_received",
                    "incident_events", ["status", "received_at"])

    # Unprocessed events for Phase 9/10 AI pipeline
    op.create_index("ix_incident_events_unprocessed",
                    "incident_events", ["processed", "received_at"],
                    postgresql_where=sa.text("processed = false"))


def downgrade() -> None:
    op.drop_table("incident_events")

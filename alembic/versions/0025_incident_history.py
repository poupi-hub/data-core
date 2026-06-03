"""incident_history + incident_patterns — Alert System V2 / Fase 7

Revision ID: 0025_incident_history
Revises: 0024_incident_events
Create Date: 2026-06-03

Duas tabelas:

incident_history
  Registro permanente de cada incidente RESOLVIDO com RCA confirmado.
  Uma linha por ocorrência confirmada — permite rastrear tendências no tempo.

incident_patterns
  Agregação por alert_id: MTTR médio, frequência, root causes top-3.
  Atualizado pelo job de agregação (horário).
  Consumido pelo RCA Engine (Fase 9) e AI Agent (Fase 10).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025_incident_history"
down_revision = "0024_incident_events"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── incident_history ──────────────────────────────────────────────────────
    # Cada linha = um incidente resolvido com root cause confirmado
    op.create_table(
        "incident_history",

        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),

        # Referência ao evento de origem
        sa.Column("incident_event_id", sa.Integer(),
                  sa.ForeignKey("incident_events.id", ondelete="SET NULL"),
                  nullable=True),

        # Identidade do alerta
        sa.Column("alert_id",   sa.String(32),  nullable=True),
        sa.Column("alertname",  sa.String(128), nullable=False),
        sa.Column("service",    sa.String(64),  nullable=True),
        sa.Column("severity",   sa.String(16),  nullable=False),
        sa.Column("category",   sa.String(32),  nullable=True),

        # Root cause confirmada
        sa.Column("root_cause",        sa.Text(),  nullable=True),
        sa.Column("root_cause_bucket", sa.String(64), nullable=True),  # categoria normalizada
        sa.Column("rca_confidence",    sa.Float(), nullable=True),      # 0.0–1.0

        # Resolução
        sa.Column("resolution",        sa.Text(),  nullable=True),   # o que foi feito
        sa.Column("resolution_type",   sa.String(32), nullable=True), # restart|config|fix|escalate|auto
        sa.Column("resolved_by",       sa.String(64), nullable=True), # human|ai_agent|auto_healing

        # Timing
        sa.Column("fired_at",         sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("recorded_at",      sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),

        # Metadados para o AI Agent
        sa.Column("ai_action_used",   sa.String(256), nullable=True),  # qual ai_action foi executado
        sa.Column("runbook",          sa.String(256), nullable=True),
        sa.Column("context_snapshot", JSONB, nullable=True),  # snapshot de métricas no momento
    )

    op.create_index("ix_ih_alert_id",   "incident_history", ["alert_id"])
    op.create_index("ix_ih_service",    "incident_history", ["service"])
    op.create_index("ix_ih_recorded",   "incident_history", ["recorded_at"])
    op.create_index("ix_ih_root_cause", "incident_history", ["root_cause_bucket"])
    op.create_index("ix_ih_event_id",   "incident_history", ["incident_event_id"])

    # ── incident_patterns ─────────────────────────────────────────────────────
    # Uma linha por alert_id — atualizada incrementalmente pelo job de agregação
    op.create_table(
        "incident_patterns",

        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),

        # Chave de agregação
        sa.Column("alert_id",  sa.String(32),  nullable=False, unique=True),
        sa.Column("alertname", sa.String(128), nullable=False),
        sa.Column("service",   sa.String(64),  nullable=True),
        sa.Column("severity",  sa.String(16),  nullable=False),

        # Frequência
        sa.Column("total_occurrences",  sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_count",     sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count",   sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_fired_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_fired_at",     sa.DateTime(timezone=True), nullable=True),

        # MTTR (Mean Time To Resolve) — em segundos
        sa.Column("mttr_seconds",       sa.Float(), nullable=True),  # média dos resolvidos
        sa.Column("mttr_p50_seconds",   sa.Float(), nullable=True),  # mediana
        sa.Column("mttr_p90_seconds",   sa.Float(), nullable=True),  # percentil 90

        # Root causes mais comuns (top-3 como JSON)
        # Ex: [{"bucket": "oom_kill", "count": 5, "pct": 0.71}, ...]
        sa.Column("top_root_causes", JSONB, nullable=True),

        # Padrão de recorrência
        sa.Column("recurrence_interval_hours", sa.Float(), nullable=True),  # avg entre ocorrências
        sa.Column("is_flapping", sa.Boolean(), nullable=False, server_default="false"),  # >3x em 24h

        # Confiança do histórico
        sa.Column("rca_confidence_avg", sa.Float(), nullable=True),

        # Timestamps de agregação
        sa.Column("last_aggregated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )

    op.create_index("ix_ip_alert_id", "incident_patterns", ["alert_id"], unique=True)
    op.create_index("ix_ip_service",  "incident_patterns", ["service"])
    op.create_index("ix_ip_mttr",     "incident_patterns", ["mttr_seconds"])
    op.create_index("ix_ip_flapping", "incident_patterns", ["is_flapping"])


def downgrade() -> None:
    op.drop_table("incident_patterns")
    op.drop_table("incident_history")

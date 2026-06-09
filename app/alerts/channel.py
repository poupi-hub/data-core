"""Alert channel definitions and routing table — Phase 11."""
from __future__ import annotations

from enum import Enum


class AlertChannel(str, Enum):
    BUSINESS = "BUSINESS"
    OPERATIONAL = "OPERATIONAL"
    EXECUTIVE = "EXECUTIVE"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Routing table: alert_type → channel
# ---------------------------------------------------------------------------

ROUTING_TABLE: dict[str, AlertChannel] = {
    # BUSINESS — quant discoveries, opportunities, shadow signals
    "shadow_signal_new": AlertChannel.BUSINESS,
    "weekly_quant_report": AlertChannel.BUSINESS,
    "edge_discovery": AlertChannel.BUSINESS,
    "opportunity": AlertChannel.BUSINESS,
    # OPERATIONAL — daily ops, readiness, gate n>=10, backlog
    "daily_quant_summary": AlertChannel.OPERATIONAL,
    "readiness_change": AlertChannel.OPERATIONAL,
    "gate_n10": AlertChannel.OPERATIONAL,
    "backlog": AlertChannel.OPERATIONAL,
    "scheduler_status": AlertChannel.OPERATIONAL,
    # EXECUTIVE — milestone gates, edge status, executive reports
    "gate_n30": AlertChannel.EXECUTIVE,
    "gate_n100": AlertChannel.EXECUTIVE,
    "edge_status_change": AlertChannel.EXECUTIVE,
    "executive_daily": AlertChannel.EXECUTIVE,
    "executive_weekly": AlertChannel.EXECUTIVE,
    # CRITICAL — infrastructure failures, metric collapse
    "api_down": AlertChannel.CRITICAL,
    "postgres_down": AlertChannel.CRITICAL,
    "redis_down": AlertChannel.CRITICAL,
    "telegram_failure": AlertChannel.CRITICAL,
    "wr_below_50": AlertChannel.CRITICAL,
    "pf_below_1_5": AlertChannel.CRITICAL,
    "edge_collapse": AlertChannel.CRITICAL,
    "stale_analytics": AlertChannel.CRITICAL,
}

# ---------------------------------------------------------------------------
# Rate limits: max messages per channel per hour
# ---------------------------------------------------------------------------

RATE_LIMITS: dict[AlertChannel, int] = {
    AlertChannel.CRITICAL: 10,
    AlertChannel.EXECUTIVE: 5,
    AlertChannel.OPERATIONAL: 20,
    AlertChannel.BUSINESS: 20,
}

# ---------------------------------------------------------------------------
# Channel env-var mapping
# ---------------------------------------------------------------------------

CHANNEL_ENV: dict[AlertChannel, str] = {
    AlertChannel.BUSINESS: "BUSINESS_CHAT_ID",
    AlertChannel.OPERATIONAL: "OPERATIONAL_CHAT_ID",
    AlertChannel.EXECUTIVE: "EXECUTIVE_CHAT_ID",
    AlertChannel.CRITICAL: "CRITICAL_CHAT_ID",
}

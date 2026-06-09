"""Phase 11 alert routing endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.deps import db_session
from app.alerts.channel import AlertChannel
from app.alerts.router import TelegramRouter

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("/routes")
def get_routes() -> dict[str, Any]:
    """Return full routing table: alert_type → channel, rate limits, env config.

    Shows which chat_id env vars are configured and which are missing.
    """
    return TelegramRouter.routing_table()


@router.post("/test")
def send_test_alert(
    db: Session = Depends(db_session),  # noqa: B008
    channel: AlertChannel = Query(  # noqa: B008
        default=AlertChannel.OPERATIONAL,
        description="Channel to test: BUSINESS | OPERATIONAL | EXECUTIVE | CRITICAL",
    ),
) -> dict[str, Any]:
    """Send a test message to the specified channel.

    Useful for verifying env vars (BUSINESS_CHAT_ID, OPERATIONAL_CHAT_ID, etc.)
    are correctly set and the bot can reach each channel.

    Note: counts toward the channel's rate limit (max per hour applies).
    Does NOT apply dedup — each call sends if rate limit allows.
    """
    router_obj = TelegramRouter()
    result = router_obj.send_test(channel, db=db)
    result["note"] = (
        "sent=false with reason=telegram_disabled means TELEGRAM_ENABLED or "
        "TELEGRAM_BOT_TOKEN is not set. "
        f"reason=no_chat_id means {channel.value}_CHAT_ID env var is missing."
    )
    return result

"""TelegramRouter — Phase 11 alert routing with dedup, rate limit, retry, fallback.

Architecture:
  - Each alert_type maps to an AlertChannel via ROUTING_TABLE
  - Each channel reads its chat_id from env (BUSINESS_CHAT_ID, etc.)
  - Global bot token: TELEGRAM_BOT_TOKEN
  - Rate limit tracked in edge_alert_state table (key: rl_{channel}_{utc_hour})
  - Dedup tracked in edge_alert_state table (key: dedup_{alert_key})
  - Retry: 2 attempts with 1 s backoff on HTTP failure
  - Fallback: structured log when all send attempts fail
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.alerts.channel import (
    CHANNEL_ENV,
    RATE_LIMITS,
    ROUTING_TABLE,
    AlertChannel,
)
from app.modules.crypto.edge.alert_state_model import EdgeAlertState

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 2
_RETRY_DELAY_S = 1.0
_TELEGRAM_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _hour_key(channel: AlertChannel) -> str:
    """Rate-limit bucket key: one bucket per channel per UTC hour."""
    h = _now_utc().strftime("%Y-%m-%dT%H")
    return f"rl_{channel.value}_{h}"


def _get_state(db: Session, key: str) -> dict | None:
    row = db.query(EdgeAlertState).filter(EdgeAlertState.alert_key == key).first()
    return row.last_value if row is not None else None


def _set_state(db: Session, key: str, value: dict) -> None:
    now = _now_utc()
    row = db.query(EdgeAlertState).filter(EdgeAlertState.alert_key == key).first()
    if row is None:
        row = EdgeAlertState(alert_key=key, last_value=value, last_sent_at=now)
        db.add(row)
    else:
        row.last_value = value
        row.last_sent_at = now
        row.updated_at = now
        db.add(row)


def _check_rate_limit(db: Session, channel: AlertChannel) -> bool:
    """Return True if within limit, False if exceeded."""
    limit = RATE_LIMITS[channel]
    key = _hour_key(channel)
    state = _get_state(db, key) or {"count": 0}
    return state.get("count", 0) < limit


def _increment_rate_limit(db: Session, channel: AlertChannel) -> None:
    key = _hour_key(channel)
    state = _get_state(db, key) or {"count": 0}
    state["count"] = state.get("count", 0) + 1
    _set_state(db, key, state)


def _send_once(token: str, chat_id: str, text: str) -> bool:
    """Single Telegram send attempt. Returns True on HTTP 200."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=_TELEGRAM_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        logger.warning(
            "telegram_router: HTTP %d for channel chat_id=%s",
            resp.status_code,
            chat_id[:8],
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_router: send exception: %s", exc)
        return False


def _send_with_retry(token: str, chat_id: str, text: str) -> bool:
    """Send with up to _RETRY_ATTEMPTS attempts and linear backoff."""
    for attempt in range(_RETRY_ATTEMPTS):
        if _send_once(token, chat_id, text):
            return True
        if attempt < _RETRY_ATTEMPTS - 1:
            time.sleep(_RETRY_DELAY_S)
    return False


# ---------------------------------------------------------------------------
# TelegramRouter
# ---------------------------------------------------------------------------


class TelegramRouter:
    """Route alert messages to the correct Telegram channel.

    Usage::

        router = TelegramRouter()
        result = router.send("wr_below_50", "<b>WR fell below 50%</b>...", db=db)
        # result: {"sent": True, "channel": "CRITICAL", "alert_type": "wr_below_50"}
    """

    def __init__(self) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("1", "true", "yes")

    def _chat_id(self, channel: AlertChannel) -> str:
        env_var = CHANNEL_ENV[channel]
        chat_id = os.getenv(env_var, "")
        if not chat_id:
            # Fallback to legacy TELEGRAM_CHAT_ID for channels that have no dedicated id yet
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        return chat_id

    def resolve_channel(self, alert_type: str) -> AlertChannel | None:
        """Return the channel for this alert_type, or None if unknown."""
        return ROUTING_TABLE.get(alert_type)

    def send(
        self,
        alert_type: str,
        text: str,
        db: Session | None = None,
        dedup_key: str | None = None,
    ) -> dict[str, Any]:
        """Route and send an alert.

        Args:
            alert_type: One of the keys in ROUTING_TABLE.
            text: HTML-formatted Telegram message body.
            db: SQLAlchemy session (required for rate-limit and dedup tracking).
            dedup_key: Optional dedup key. When provided and already sent, skip.

        Returns:
            dict with keys: sent, channel, alert_type, reason (on skip), error (on fail).
        """
        channel = self.resolve_channel(alert_type)
        if channel is None:
            logger.warning("telegram_router: unknown alert_type=%s — no route", alert_type)
            return {
                "sent": False,
                "channel": None,
                "alert_type": alert_type,
                "reason": "no_route",
            }

        if not self._enabled or not self._token:
            logger.debug("telegram_router: disabled or no token — skipping %s", alert_type)
            return {
                "sent": False,
                "channel": channel.value,
                "alert_type": alert_type,
                "reason": "telegram_disabled",
            }

        # --- Dedup check ---
        if dedup_key and db is not None:
            state = _get_state(db, f"dedup_{dedup_key}")
            if state and state.get("sent"):
                return {
                    "sent": False,
                    "channel": channel.value,
                    "alert_type": alert_type,
                    "reason": "dedup",
                }

        # --- Rate limit check ---
        if db is not None and not _check_rate_limit(db, channel):
            limit = RATE_LIMITS[channel]
            logger.warning(
                "telegram_router: rate limit hit channel=%s limit=%d/h",
                channel.value,
                limit,
            )
            return {
                "sent": False,
                "channel": channel.value,
                "alert_type": alert_type,
                "reason": "rate_limited",
                "rate_limit_per_hour": limit,
            }

        # --- Send ---
        chat_id = self._chat_id(channel)
        if not chat_id:
            logger.warning("telegram_router: no chat_id for channel=%s", channel.value)
            return {
                "sent": False,
                "channel": channel.value,
                "alert_type": alert_type,
                "reason": "no_chat_id",
            }

        sent = _send_with_retry(self._token, chat_id, text)

        if not sent:
            # Fallback log
            logger.error(
                "telegram_router: FALLBACK — failed to send "
                "alert_type=%s channel=%s | text_preview=%s",
                alert_type,
                channel.value,
                text[:120],
            )
            return {
                "sent": False,
                "channel": channel.value,
                "alert_type": alert_type,
                "reason": "send_failed",
            }

        # --- Update state ---
        if db is not None:
            _increment_rate_limit(db, channel)
            if dedup_key:
                _set_state(db, f"dedup_{dedup_key}", {"sent": True, "at": _now_utc().isoformat()})
            db.commit()

        logger.info("telegram_router: sent alert_type=%s channel=%s", alert_type, channel.value)
        return {
            "sent": True,
            "channel": channel.value,
            "alert_type": alert_type,
        }

    def send_test(self, channel: AlertChannel, db: Session | None = None) -> dict[str, Any]:
        """Send a test ping to a specific channel (bypasses routing table and dedup).

        Counts toward the channel's hourly rate limit.
        """
        if not self._enabled or not self._token:
            return {
                "sent": False,
                "channel": channel.value,
                "alert_type": "test",
                "reason": "telegram_disabled",
            }

        if db is not None and not _check_rate_limit(db, channel):
            return {
                "sent": False,
                "channel": channel.value,
                "alert_type": "test",
                "reason": "rate_limited",
                "rate_limit_per_hour": RATE_LIMITS[channel],
            }

        chat_id = self._chat_id(channel)
        if not chat_id:
            return {
                "sent": False,
                "channel": channel.value,
                "alert_type": "test",
                "reason": "no_chat_id",
            }

        text = (
            f"<b>[Test] Canal: {channel.value}</b>\n"
            f"Routing OK — Phase 11 TelegramRouter\n"
            f"<i>{_now_utc().strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )
        sent = _send_with_retry(self._token, chat_id, text)

        if sent and db is not None:
            _increment_rate_limit(db, channel)
            db.commit()

        return {
            "sent": sent,
            "channel": channel.value,
            "alert_type": "test",
            "reason": "send_failed" if not sent else None,
        }

    @staticmethod
    def routing_table() -> dict[str, Any]:
        """Return full routing table and rate limits as a serialisable dict."""
        by_channel: dict[str, list[str]] = {c.value: [] for c in AlertChannel}
        for alert_type, channel in ROUTING_TABLE.items():
            by_channel[channel.value].append(alert_type)

        return {
            "channels": {
                c.value: {
                    "alert_types": by_channel[c.value],
                    "rate_limit_per_hour": RATE_LIMITS[c],
                    "chat_id_env_var": CHANNEL_ENV[c],
                    "chat_id_configured": bool(os.getenv(CHANNEL_ENV[c], "")),
                }
                for c in AlertChannel
            },
            "total_alert_types": len(ROUTING_TABLE),
        }

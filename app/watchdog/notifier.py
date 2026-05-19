"""TelegramNotifier — sends messages to Telegram Bot API.

Uses httpx (sync) directly.  Designed to be used from the watchdog scheduler job
which runs in a background thread (not inside an event loop).

Configuration (core.config.Settings):
  telegram_enabled     — must be True to send; silently no-ops if False
  telegram_bot_token   — Telegram bot token
  telegram_chat_id     — destination chat ID (group or personal)

All errors are caught and logged — the notifier must NEVER crash the watchdog.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 15.0  # seconds


class TelegramNotifier:
    """Send plain-text or HTML-formatted messages to a Telegram chat.

    Usage::

        notifier = TelegramNotifier()
        ok = notifier.send("🔴 <b>Alerta crítico</b>: sem coleta por 4h")
        notifier.send_plain("Sistema ok")
    """

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self._token = bot_token or settings.telegram_bot_token
        self._chat_id = chat_id or settings.telegram_chat_id
        self._enabled = settings.telegram_enabled and bool(self._token) and bool(self._chat_id)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def send(self, html_text: str, disable_preview: bool = True) -> bool:
        """Send an HTML-formatted message.  Returns True on success."""
        return self._post(html_text, parse_mode="HTML", disable_preview=disable_preview)

    def send_plain(self, text: str) -> bool:
        """Send a plain-text message (no HTML escaping needed)."""
        return self._post(text, parse_mode=None, disable_preview=True)

    def _post(
        self,
        text: str,
        parse_mode: str | None,
        disable_preview: bool,
    ) -> bool:
        if not self._enabled:
            logger.debug(
                "Telegram notifier disabled — message suppressed",
                extra={"text_preview": text[:80]},
            )
            return False

        url = _TELEGRAM_API.format(token=self._token)
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
            if resp.status_code == 200:
                logger.debug("Telegram message sent", extra={"chat_id": self._chat_id})
                return True
            else:
                logger.warning(
                    "Telegram API returned non-200",
                    extra={"status": resp.status_code, "body": resp.text[:200]},
                )
                return False
        except httpx.TimeoutException:
            logger.warning("Telegram send timed out", extra={"timeout": _TIMEOUT})
            return False
        except Exception:
            logger.exception("Telegram send failed unexpectedly")
            return False

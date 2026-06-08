"""Critical alert notifier for auto-healing.

Sends Telegram only when healing fails or needs human action.
Policy: RECOVERED=silent, FAILED=notify, SKIPPED+critical=notify.
"""

from __future__ import annotations

import logging

import httpx

from app.auto_healing.models import HealOutcome, HealResult, ServiceHealth

logger = logging.getLogger(__name__)

_ICONS = {
    HealOutcome.FAILED: "FAIL",
    HealOutcome.SKIPPED: "SKIP",
    HealOutcome.RECOVERED: "OK",
}

_CRITICAL_SERVICES = frozenset(
    {"postgres", "redis", "scheduler", "workers", "data-core", "poupi-crypto", "poupi-baby"}
)


def should_notify(result: HealResult, health: ServiceHealth | None = None) -> bool:
    if result.outcome == HealOutcome.RECOVERED:
        return False
    if result.outcome == HealOutcome.FAILED:
        return True
    return result.service in _CRITICAL_SERVICES


class CriticalNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    def notify(self, results: list[HealResult], health_map: dict[str, ServiceHealth]) -> None:
        if not self._bot_token or not self._chat_id:
            logger.debug("notifier: telegram not configured, skipping")
            return
        alerts = [r for r in results if should_notify(r, health_map.get(r.service))]
        if not alerts:
            return
        lines = ["Auto-Healing Alert", ""]
        for result in alerts:
            icon = _ICONS.get(result.outcome, "?")
            lines.append(f"[{icon}] {result.service}: {result.outcome.value}")
            lines.append(f"   {result.detail}")
            if result.error:
                lines.append(f"   Erro: {result.error[:120]}")
        lines.extend(["", "Acao requerida: verificar servico manualmente."])
        self._send("\n".join(lines))

    def _send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            response = httpx.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
            if response.status_code != 200:
                logger.warning("notifier: telegram returned %d", response.status_code)
        except Exception as exc:
            logger.warning("notifier: telegram send failed: %s", exc)


def build_notifier() -> CriticalNotifier | None:
    from core.config import settings

    token = getattr(settings, "telegram_bot_token", "")
    chat_id = getattr(settings, "telegram_chat_id", "")
    if not token or not chat_id:
        return None
    return CriticalNotifier(bot_token=token, chat_id=chat_id)

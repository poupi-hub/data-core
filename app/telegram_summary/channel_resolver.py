"""Telegram channel resolver — canal centralizado.

Todos os alertas e summaries vão para TELEGRAM_SYSTEM_CHAT_ID (ou telegram_chat_id
como fallback). Os labels identificam a origem no canal compartilhado.

Routing table (destino único):
  operational / system_critical / safe_mode / low_replayability → label [POUPI OPS]
  quant / longitudinal / quant_critical / low_confidence        → label [CRYPTO]
"""

from __future__ import annotations

from core.config import settings

_CRITICAL_OPS_TYPES: frozenset[str] = frozenset({
    "system_critical",
    "safe_mode_activated",
    "quant_critical",
})

_OPS_TYPES: frozenset[str] = frozenset({
    "operational",
    "low_replayability",
    "low_confidence",
})

_RESEARCH_TYPES: frozenset[str] = frozenset({
    "quant",
    "longitudinal",
})

_LABELS: dict[str, str] = {
    "critical_ops": "<b>[CRITICAL OPERATIONS]</b>\n",
    "ops": "<b>[OPERATIONS]</b>\n",
    "research": "<b>[RESEARCH]</b>\n",
}


def resolve_chat_id(_event_type: str) -> str:
    """Retorna o chat_id do canal centralizado (TELEGRAM_SYSTEM_CHAT_ID).

    O parâmetro event_type é ignorado — todos os eventos vão para o mesmo canal.
    """
    return settings.telegram_system_chat_id or settings.telegram_chat_id


def resolve_label(event_type: str) -> str:
    """Retorna o label HTML que identifica a origem da mensagem no canal compartilhado."""
    if event_type in _CRITICAL_OPS_TYPES:
        return _LABELS["critical_ops"]
    if event_type in _OPS_TYPES:
        return _LABELS["ops"]
    if event_type in _RESEARCH_TYPES:
        return _LABELS["research"]
    return ""

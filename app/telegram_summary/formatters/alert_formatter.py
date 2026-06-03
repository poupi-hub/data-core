"""Format immediate Telegram alerts as action-oriented HTML messages."""

from __future__ import annotations

from app.telegram_summary.dto import AlertPayload

_SEVERITY_ICON: dict[str, str] = {
    "warning": "&#9888;&#65039;",
    "critical": "&#128680;",
}


def format_alert(payload: AlertPayload) -> str:
    """Return an HTML-formatted immediate alert message."""
    icon = _SEVERITY_ICON.get(payload.severity, "?")
    ts = payload.generated_at.strftime("%d/%m/%Y %H:%M UTC")
    urgency = "CRITICA" if payload.severity == "critical" else "MEDIA"
    evidence = "; ".join(
        f"{key}={value}" for key, value in list((payload.details or {}).items())[:5]
    ) or payload.alert_type

    lines: list[str] = [
        f"{icon} <b>ALERTA</b>",
        "",
        "<b>Sistema:</b>",
        "Data Core / Operational Truth",
        "",
        "<b>Problema:</b>",
        payload.title,
        "",
        "<b>Impacto:</b>",
        payload.message,
        "",
        "<b>Urgencia:</b>",
        urgency,
        "",
        "<b>Acao:</b>",
        "Verificar /health/operational, dashboards operacionais e logs do componente afetado.",
        "",
        "<b>Evidencia:</b>",
        evidence,
        "",
        "<b>Dashboard:</b>",
        "Data Core / Operational Truth",
        "",
        f"<i>{ts} - data-core</i>",
    ]
    return "\n".join(lines)

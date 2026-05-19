"""HeartbeatFormatter — builds the periodic Telegram status summary message.

Format (HTML for Telegram):

✅ Poupi saudável — 2026-05-18 14:30

📦 Coleta: OK
  • 3 fontes ativas nas últimas 3h
  • Última coleta: há 45 min

🔄 Normalização: OK
  • 45 registros normalizados nas 24h
  • Taxa de sucesso: 96%

📣 Telegram: OK
  • Última publicação: há 2h
  • Enviados 24h: 5 | Falhas: 0

🔍 Qualidade: OK
  • Fontes monitoradas: 3
  • Drift aberto: 0

🕐 Próximo heartbeat: em ~6h
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.watchdog.checks import CheckResult

_STATUS_EMOJI = {"ok": "✅", "warning": "⚠️", "critical": "🔴"}
_STATUS_PT = {"ok": "OK", "warning": "ATENÇÃO", "critical": "CRÍTICO"}


def _fmt_age(seconds: int | float | None) -> str:
    if seconds is None:
        return "desconhecido"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3600:.1f}h"


class HeartbeatFormatter:
    """Build the Telegram heartbeat summary from a list of CheckResult objects."""

    def format(
        self,
        check_results: list[CheckResult],
        heartbeat_interval_hours: int = 6,
    ) -> str:
        now = datetime.now(tz=timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M UTC")

        overall = _overall_status(check_results)
        emoji = _STATUS_EMOJI[overall]
        title_status = "Poupi saudável" if overall == "ok" else f"Poupi — {_STATUS_PT[overall]}"

        lines: list[str] = [
            f"{emoji} <b>{title_status}</b>",
            f"<i>{ts}</i>",
            "",
        ]

        for result in check_results:
            lines.extend(self._format_check(result))
            lines.append("")

        lines.append(f"🕐 Próximo heartbeat: em ~{heartbeat_interval_hours}h")

        # Alert summary if any
        all_alerts = [a for r in check_results for a in r.alerts]
        if all_alerts:
            lines.append("")
            lines.append("⚠️ <b>Alertas ativos:</b>")
            for a in all_alerts[:5]:  # max 5 in heartbeat
                sev_emoji = "🔴" if a.severity == "critical" else "⚠️"
                lines.append(f"  {sev_emoji} {a.title}")
            if len(all_alerts) > 5:
                lines.append(f"  ... +{len(all_alerts) - 5} outros")

        return "\n".join(lines)

    def _format_check(self, result: CheckResult) -> list[str]:
        emoji = _STATUS_EMOJI[result.status]
        name_map = {
            "collection": "Coleta",
            "normalization": "Normalização",
            "scraper_quality": "Qualidade",
            "telegram": "Telegram",
        }
        name = name_map.get(result.name, result.name.title())
        status_pt = _STATUS_PT[result.status]
        lines = [f"{emoji} <b>{name}: {status_pt}</b>"]

        m = result.metrics or {}

        if result.name == "collection":
            active = m.get("active_sources_last_window", "?")
            stale_h = m.get("last_raw_collection_age_seconds")
            lines.append(f"  • Fontes ativas: {active}")
            if stale_h is not None:
                lines.append(f"  • Última coleta: há {_fmt_age(stale_h)}")
            stale_srcs = m.get("stale_sources", [])
            if stale_srcs:
                lines.append(f"  • Sem coleta: {', '.join(stale_srcs)}")

        elif result.name == "normalization":
            src_rates = m.get("source_rates", {})
            total_norm = sum(v.get("normalized", 0) for v in src_rates.values())
            pending = m.get("normalization_pending_total", 0)
            age_secs = m.get("last_normalized_age_seconds")
            lines.append(f"  • Normalizados 24h: {total_norm}")
            lines.append(f"  • Pendentes: {pending}")
            if age_secs is not None:
                lines.append(f"  • Último normalizado: há {_fmt_age(age_secs)}")

        elif result.name == "scraper_quality":
            qs = m.get("quality_stats", {})
            ab = m.get("anti_bot_by_source_1h", {})
            drift = m.get("open_drift_events", [])
            lines.append(f"  • Fontes monitoradas: {len(qs)}")
            if qs:
                avg_scores = [v["avg_score"] for v in qs.values()]
                overall_avg = sum(avg_scores) / len(avg_scores)
                lines.append(f"  • Qualidade média: {overall_avg:.0f}/100")
            ab_total = sum(ab.values())
            lines.append(f"  • Anti-bot 1h: {ab_total}")
            lines.append(f"  • Drift aberto: {len(drift)}")

        elif result.name == "telegram":
            sent_24h = m.get("telegram_sent_24h", 0)
            failed_24h = m.get("telegram_failed_24h", 0)
            age_secs = m.get("last_telegram_post_age_seconds")
            note = m.get("status_note", "")
            if note == "no_callback_data":
                lines.append("  • Callback não configurado")
            else:
                lines.append(f"  • Enviados 24h: {sent_24h} | Falhas: {failed_24h}")
                if age_secs is not None:
                    lines.append(f"  • Última publicação: há {_fmt_age(age_secs)}")

        return lines


def format_alert_message(alert_code: str, title: str, message: str, severity: str) -> str:
    """Format a standalone critical/warning alert Telegram message."""
    emoji = "🔴" if severity == "critical" else "⚠️"
    return (
        f"{emoji} <b>{title}</b>\n\n"
        f"{message}\n\n"
        f"<code>code: {alert_code}</code>\n"
        f"<i>{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>"
    )


def _overall_status(results: list[CheckResult]) -> str:
    statuses = {r.status for r in results}
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    return "ok"

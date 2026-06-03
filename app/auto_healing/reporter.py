from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from app.auto_healing.models import Classification, WatchdogExecution
from app.watchdog.notifier import TelegramNotifier
from core.config import settings

logger = logging.getLogger(__name__)

_TELEGRAM_SAFE_LIMIT = 3900


class AutoHealingReporter:
    def format(self, execution: WatchdogExecution) -> str:
        local_ts = execution.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
        counts = Counter(item.classification for item in execution.alerts_analyzed)
        actions = execution.actions or []
        pending = execution.manual_pending or []

        lines = [
            f"AUTO-HEALING WATCHDOG - {local_ts}",
            "",
            "Status geral:",
            execution.status.value,
            "",
            "Alertas analisados:",
            f"- total: {len(execution.alerts_analyzed)}",
            f"- reais: {counts[Classification.REAL]}",
            f"- falsos positivos: {counts[Classification.FALSO_POSITIVO]}",
            f"- recuperados: {counts[Classification.RECUPERADO]}",
            f"- inconclusivos: {counts[Classification.INCONCLUSIVO]}",
            "",
            "Correcoes aplicadas:",
        ]
        if actions:
            lines.extend(f"- {a.name} [{a.status}] {a.target}: {a.result or a.evidence}" for a in actions[:8])
        else:
            lines.append("- nenhuma")

        lines.extend(["", "Pendencias manuais:"])
        if pending:
            lines.extend(f"- {item}" for item in pending[:8])
        else:
            lines.append("- nenhuma")

        if execution.dry_run:
            lines.extend(["", "Modo: DRY_RUN"])
        if execution.errors:
            lines.extend(["", "Erros:"])
            lines.extend(f"- {err}" for err in execution.errors[:5])
        return "\n".join(lines)

    def send(self, execution: WatchdogExecution) -> bool:
        if not settings.auto_healing_telegram_report:
            return False
        state_path = _telegram_state_path()
        if _cooldown_active(state_path, settings.auto_healing_telegram_cooldown_minutes):
            logger.info("auto_healing: Telegram report suppressed by cooldown")
            return False

        try:
            notifier = TelegramNotifier(chat_id=settings.telegram_system_chat_id or settings.telegram_chat_id)
            sent = notifier.send_plain(_truncate_telegram_message(self.format(execution)))
            if sent:
                _write_telegram_state(state_path)
            return sent
        except Exception as exc:
            logger.warning("auto_healing: Telegram report send failed: %s", exc)
            return False


def _truncate_telegram_message(message: str) -> str:
    if len(message) <= _TELEGRAM_SAFE_LIMIT:
        return message
    suffix = "\n\n[truncated]"
    return message[: _TELEGRAM_SAFE_LIMIT - len(suffix)] + suffix


def _telegram_state_path() -> Path:
    history_path = Path(settings.auto_healing_history_path)
    return history_path.with_name(f"{history_path.name}.telegram_state.json")


def _cooldown_active(path: Path, cooldown_minutes: int) -> bool:
    if cooldown_minutes <= 0:
        return False
    try:
        if not path.exists():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        sent_at_raw = payload.get("last_sent_at")
        if not sent_at_raw:
            return False
        sent_at = datetime.fromisoformat(sent_at_raw)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - sent_at).total_seconds()
        return age_seconds < cooldown_minutes * 60
    except Exception:
        return False


def _write_telegram_state(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_sent_at": datetime.now(timezone.utc).isoformat()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("auto_healing: failed to persist Telegram cooldown state: %s", exc)

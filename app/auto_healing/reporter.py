from __future__ import annotations

from collections import Counter

from app.auto_healing.models import WatchdogExecution


class AutoHealingReporter:
    """Formats local dry-run summaries only.

    Phase 2 must not send Telegram or trigger any external notification path.
    """

    def format(self, execution: WatchdogExecution) -> str:
        local_ts = execution.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
        counts = Counter(item.classification for item in execution.events)
        lines = [
            f"AUTO-HEALING WATCHDOG - {local_ts}",
            "",
            "Status geral:",
            execution.status.value,
            "",
            "Eventos dry-run:",
            f"- total: {len(execution.events)}",
        ]
        for classification, count in sorted(counts.items(), key=lambda item: item[0].value):
            lines.append(f"- {classification.value}: {count}")
        lines.extend(["", "Modo: DRY_RUN STRICT"])
        if execution.errors:
            lines.extend(["", "Erros:"])
            lines.extend(f"- {err}" for err in execution.errors[:5])
        return "\n".join(lines)

    def send(self, execution: WatchdogExecution) -> bool:
        return False

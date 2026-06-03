"""
LogsCollector — coleta linhas de log recentes dos serviços afetados.

Estratégias (em ordem de preferência):
  1. PM2 log files (Linux VPS) — lê diretamente o arquivo de log
  2. journald via journalctl (Linux systemd)
  3. Docker logs (se disponível)
  4. Fallback: retorna empty com aviso

Foco em:
  - Últimas N linhas do stderr/stdout
  - Linhas contendo ERROR, EXCEPTION, CRITICAL, OOM, KILLED
  - Últimos 15 minutos
"""

from __future__ import annotations

import os
import subprocess
import re
from pathlib import Path
from typing import Any

from app.context_builder.collectors.base import BaseCollector

# Mapeamento service → nome do processo PM2 / service systemd
_SERVICE_PM2_NAMES: dict[str, str] = {
    "data-core":        "data-core-api",
    "data-core-sched":  "data-core-scheduler",
    "poupi-crypto":     "poupi-crypto-core-15m",
    "poupi-baby":       "poupi-baby-backend",
    "poupi-baby-worker":"poupi-baby-worker",
}

# Padrões de log relevantes
_ERROR_PATTERNS = re.compile(
    r"(error|exception|critical|fatal|oom|killed|traceback|failed|panic|sigkill|sigterm)",
    re.IGNORECASE,
)

_MAX_LOG_LINES = 50
_MAX_ERROR_LINES = 20
_MAX_LINE_LENGTH = 300


class LogsCollector(BaseCollector):
    name = "logs"
    timeout_seconds = 8.0

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        service = context.get("service", "")
        pm2_name = _SERVICE_PM2_NAMES.get(service)
        if not pm2_name:
            # Tentar inferir pelo nome do serviço
            pm2_name = service.replace("/", "-")

        all_lines: list[str] = []
        error_lines: list[str] = []
        source_used = "none"
        error_msg: str | None = None

        # ── Estratégia 1: PM2 logs ────────────────────────────────────────────
        try:
            result = subprocess.run(
                ["pm2", "logs", pm2_name, "--lines", str(_MAX_LOG_LINES), "--nostream"],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds - 1,
            )
            if result.returncode == 0:
                raw = (result.stdout + result.stderr).splitlines()
                all_lines = [line[:_MAX_LINE_LENGTH] for line in raw[-_MAX_LOG_LINES:]]
                error_lines = [l for l in all_lines if _ERROR_PATTERNS.search(l)][-_MAX_ERROR_LINES:]
                source_used = "pm2"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            error_msg = f"pm2 unavailable: {exc}"

        # ── Estratégia 2: journald ────────────────────────────────────────────
        if not all_lines and source_used == "none":
            try:
                result = subprocess.run(
                    [
                        "journalctl",
                        "-u", pm2_name,
                        "--since", "15 minutes ago",
                        "--no-pager",
                        "-n", str(_MAX_LOG_LINES),
                        "--output", "short",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds - 1,
                )
                if result.returncode == 0:
                    all_lines = [l[:_MAX_LINE_LENGTH] for l in result.stdout.splitlines()[-_MAX_LOG_LINES:]]
                    error_lines = [l for l in all_lines if _ERROR_PATTERNS.search(l)][-_MAX_ERROR_LINES:]
                    source_used = "journald"
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                error_msg = f"journald unavailable: {exc}"

        # ── Estratégia 3: dmesg para erros de OOM ────────────────────────────
        dmesg_lines: list[str] = []
        try:
            result = subprocess.run(
                ["dmesg", "--time-format", "iso", "--level", "err,crit"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                dmesg_lines = [
                    l[:_MAX_LINE_LENGTH]
                    for l in result.stdout.splitlines()[-20:]
                    if _ERROR_PATTERNS.search(l)
                ]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        return {
            "service": service,
            "pm2_name": pm2_name,
            "source": source_used,
            "recent_lines_count": len(all_lines),
            "recent_lines": all_lines[-20:],   # últimas 20 linhas
            "error_lines": error_lines,          # linhas com padrão de erro
            "dmesg_errors": dmesg_lines,         # erros do kernel (OOM, etc.)
            "has_errors": len(error_lines) > 0,
            "has_oom": any("oom" in l.lower() or "killed" in l.lower() for l in dmesg_lines),
            "collection_error": error_msg,
        }

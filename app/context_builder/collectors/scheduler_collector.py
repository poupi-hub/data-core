"""
SchedulerCollector — coleta estado atual do APScheduler no data-core.

Usa a API interna do scheduler para obter:
  - Estado atual (healthy, degraded, frozen, oom)
  - Heartbeat age
  - Jobs em execução
  - Últimas falhas
  - Restart count
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from app.context_builder.collectors.base import BaseCollector

_SCHEDULER_API_BASE = "http://localhost:8000"

_ENDPOINTS = [
    ("state",     f"{_SCHEDULER_API_BASE}/api/v1/runtime/scheduler-diagnosis"),
    ("heartbeat", f"{_SCHEDULER_API_BASE}/metrics"),  # raw Prometheus metrics
]

_SCHEDULER_STATE_NAMES = {
    0: "healthy",
    1: "degraded",
    2: "stale",
    3: "frozen",
    4: "oom_recent",
}


class SchedulerCollector(BaseCollector):
    name = "scheduler"
    timeout_seconds = 6.0

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        service = context.get("service", "")

        # Apenas relevante para data-core
        if service not in ("data-core", ""):
            return {"service": service, "relevant": False}

        results: dict[str, Any] = {}
        errors: list[str] = []

        # ── Diagnóstico via API ───────────────────────────────────────────────
        try:
            req = urllib.request.Request(
                f"{_SCHEDULER_API_BASE}/api/v1/runtime/scheduler-diagnosis",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                diagnosis = json.loads(resp.read(8192).decode())
                results["diagnosis"] = diagnosis
        except Exception as exc:
            errors.append(f"scheduler diagnosis: {exc}")

        # ── Heartbeat age via Prometheus metrics ──────────────────────────────
        try:
            with urllib.request.urlopen(
                f"{_SCHEDULER_API_BASE}/metrics",
                timeout=3
            ) as resp:
                metrics_text = resp.read(65536).decode("utf-8", errors="replace")
                heartbeat_age = self._extract_metric(
                    metrics_text, "scheduler_heartbeat_age_seconds"
                )
                restart_count = self._extract_metric(
                    metrics_text, "data_core_scheduler_restart_count"
                )
                memory_ratio = self._extract_metric(
                    metrics_text, "data_core_scheduler_memory_usage_ratio"
                )
                sched_state = self._extract_metric(
                    metrics_text, "data_core_scheduler_state"
                )

                results["metrics"] = {
                    "heartbeat_age_seconds": heartbeat_age,
                    "restart_count": restart_count,
                    "memory_usage_ratio": memory_ratio,
                    "state": sched_state,
                    "state_name": _SCHEDULER_STATE_NAMES.get(
                        int(sched_state) if sched_state is not None else 0, "unknown"
                    ),
                }
        except Exception as exc:
            errors.append(f"metrics: {exc}")

        # ── Derived signals ───────────────────────────────────────────────────
        is_frozen = False
        is_oom = False
        heartbeat_stale = False

        if "metrics" in results:
            m = results["metrics"]
            is_frozen = (m.get("state", 0) or 0) >= 2
            is_oom = (m.get("state", 0) or 0) == 4
            heartbeat_stale = (m.get("heartbeat_age_seconds", 0) or 0) > 600

        return {
            "service": service,
            "relevant": True,
            "is_frozen": is_frozen,
            "is_oom": is_oom,
            "heartbeat_stale": heartbeat_stale,
            "data": results,
            "errors": errors,
        }

    @staticmethod
    def _extract_metric(text: str, metric_name: str) -> float | None:
        """Extrai o valor de uma métrica do formato Prometheus text."""
        for line in text.splitlines():
            if line.startswith(metric_name) and not line.startswith("#"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return float(parts[-1])
                    except ValueError:
                        pass
        return None

"""
ContextBuilderService — orquestra os collectors em paralelo.

Fluxo:
  1. Recebe um IncidentEvent
  2. Determina quais collectors executar baseado em ai_action
  3. Executa em paralelo com ThreadPoolExecutor
  4. Gera hipóteses via HypothesisGenerator
  5. Persiste o context_snapshot no IncidentEvent
  6. Retorna o snapshot completo

Cada collector:
  - Tem timeout próprio
  - Nunca bloqueia os demais
  - Falhas são capturadas e incluídas no snapshot
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.context_builder.collectors.base import BaseCollector, CollectorResult
from app.context_builder.collectors.deploy_collector import DeployCollector
from app.context_builder.collectors.health_collector import HealthCollector
from app.context_builder.collectors.logs_collector import LogsCollector
from app.context_builder.collectors.metrics_collector import MetricsCollector
from app.context_builder.collectors.postgres_collector import PostgresCollector
from app.context_builder.collectors.redis_collector import RedisCollector
from app.context_builder.collectors.scheduler_collector import SchedulerCollector
from app.context_builder.hypothesis_generator import HypothesisGenerator
from app.context_builder.metrics import (
    CONTEXT_BUILT,
    CONTEXT_BUILD_DURATION,
    CONTEXT_BUILD_ERRORS,
)
from app.incident_bus.models import IncidentEvent

logger = logging.getLogger(__name__)

# Mapeamento ai_action → collector
_ACTION_COLLECTOR_MAP: dict[str, type[BaseCollector]] = {
    "check_health":    HealthCollector,
    "check_metrics":   MetricsCollector,
    "check_logs":      LogsCollector,
    "check_redis":     RedisCollector,
    "check_postgres":  PostgresCollector,
    "check_deploy":    DeployCollector,
    "check_scheduler": SchedulerCollector,
}

# Collectors que sempre rodam (independente de ai_action)
_DEFAULT_COLLECTORS: list[type[BaseCollector]] = [
    HealthCollector,
    MetricsCollector,
]

# Timeout global para o build inteiro
_GLOBAL_TIMEOUT_SECONDS = 30.0
_MAX_WORKERS = 5


class ContextBuilderService:

    def __init__(self) -> None:
        self._hypothesis_generator = HypothesisGenerator()

    def build(self, event: IncidentEvent) -> dict[str, Any]:
        """
        Constrói o context snapshot para um IncidentEvent.
        Retorna o snapshot sem persistir (persistência via build_and_save).
        """
        t0 = time.perf_counter()

        context = self._build_collection_context(event)
        collectors = self._resolve_collectors(event.ai_action)
        results: dict[str, CollectorResult] = {}

        # Execução paralela dos collectors
        with ThreadPoolExecutor(max_workers=min(len(collectors), _MAX_WORKERS)) as executor:
            futures = {
                executor.submit(c.collect, context): c.name
                for c in collectors
            }
            for future in as_completed(futures, timeout=_GLOBAL_TIMEOUT_SECONDS):
                name = futures[future]
                try:
                    result = future.result(timeout=1.0)
                    results[name] = result
                except FutureTimeout:
                    results[name] = CollectorResult(
                        source=name, success=False,
                        error=f"Collector timed out after {_GLOBAL_TIMEOUT_SECONDS}s"
                    )
                except Exception as exc:
                    results[name] = CollectorResult(
                        source=name, success=False,
                        error=f"Unhandled: {exc}"
                    )

        # Gerar hipóteses
        sources_dict = {name: r.to_dict() for name, r in results.items()}
        hypotheses = self._hypothesis_generator.generate({"sources": sources_dict})

        # Montar snapshot final
        duration_ms = (time.perf_counter() - t0) * 1000
        snapshot = {
            "alert_id":       event.alert_id,
            "alertname":      event.alertname,
            "service":        event.service,
            "severity":       event.severity,
            "status":         event.status,
            "collected_at":   datetime.now(timezone.utc).isoformat(),
            "duration_ms":    round(duration_ms, 1),
            "collectors_run": list(results.keys()),
            "sources":        sources_dict,
            "affected_services": self._identify_affected_services(event, results),
            "hypotheses":     hypotheses,
            "top_hypothesis":  hypotheses[0] if hypotheses else None,
        }

        CONTEXT_BUILT.labels(service=event.service or "unknown", severity=event.severity).inc()
        CONTEXT_BUILD_DURATION.observe(duration_ms / 1000)

        return snapshot

    def build_and_save(self, event: IncidentEvent, db: Session) -> dict[str, Any]:
        """Constrói o contexto e persiste no IncidentEvent.context_snapshot."""
        try:
            snapshot = self.build(event)
            event.context_snapshot = snapshot
            db.flush()
            return snapshot
        except Exception as exc:
            CONTEXT_BUILD_ERRORS.labels(alertname=event.alertname).inc()
            logger.exception(
                "Context builder failed for event %s (%s): %s",
                event.id, event.alertname, exc
            )
            error_snapshot = {
                "alert_id": event.alert_id,
                "error": str(exc),
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
            event.context_snapshot = error_snapshot
            db.flush()
            return error_snapshot

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_collection_context(self, event: IncidentEvent) -> dict[str, Any]:
        """Extrai o contexto do evento para passar aos collectors."""
        return {
            "alert_id":    event.alert_id,
            "alertname":   event.alertname,
            "service":     event.service,
            "severity":    event.severity,
            "category":    event.category,
            "component":   event.component,
            "layer":       event.layer,
            "runtime":     event.runtime,
            "ai_action":   event.ai_action,
            "fired_at":    event.fired_at,
            "labels":      event.labels or {},
            "annotations": event.annotations or {},
        }

    def _resolve_collectors(self, ai_action: str | None) -> list[BaseCollector]:
        """
        Determina quais collectors executar baseado no ai_action do alerta.
        Exemplo: "check_logs,check_health,check_redis"
        """
        collector_classes: set[type[BaseCollector]] = set(_DEFAULT_COLLECTORS)

        if ai_action:
            for action in ai_action.split(","):
                action = action.strip()
                if action in _ACTION_COLLECTOR_MAP:
                    collector_classes.add(_ACTION_COLLECTOR_MAP[action])

        return [cls() for cls in collector_classes]

    @staticmethod
    def _identify_affected_services(
        event: IncidentEvent,
        results: dict[str, CollectorResult],
    ) -> list[str]:
        """Identifica serviços afetados além do serviço primário."""
        affected = []
        if event.service:
            affected.append(event.service)

        # Se health de um serviço secundário também falhou
        health_result = results.get("health")
        if health_result and health_result.success:
            h_data = health_result.data
            for endpoint_name, endpoint_data in h_data.get("results", {}).items():
                if isinstance(endpoint_data, dict):
                    status = endpoint_data.get("status_code", 200)
                    if status >= 500:
                        # Inferir serviço do endpoint name
                        affected.append(f"{event.service}/{endpoint_name}")

        # Redis down → afeta todos que dependem de Redis
        redis_result = results.get("redis")
        if redis_result and redis_result.success:
            if not redis_result.data.get("all_connected", True):
                for svc in ["poupi-crypto", "poupi-baby"]:
                    if svc not in affected:
                        affected.append(svc + " (redis dependency)")

        return list(set(affected))

"""
MetricsCollector — consulta o Prometheus para métricas relevantes ao alerta.

Queries adaptadas por alert_id/service.
Retorna valores atuais das métricas mais importantes para o diagnóstico.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.parse
from typing import Any

from app.context_builder.collectors.base import BaseCollector

# URL padrão do Prometheus (ajustável via env)
_PROMETHEUS_URL = "http://localhost:9090"

# Queries relevantes por service
_METRIC_QUERIES: dict[str, list[tuple[str, str]]] = {
    "data-core": [
        ("scheduler_heartbeat_age_seconds", "scheduler_heartbeat_age_seconds"),
        ("queue_backlog_ecommerce", "queue_backlog_total{module='ecommerce'}"),
        ("pipeline_lag_normalize", "pipeline_liveness_lag_seconds{pipeline_id='normalize_ecommerce'}"),
        ("analytics_freshness_crypto", "analytics_freshness_seconds{module='crypto'}"),
        ("scheduler_memory", "data_core_scheduler_memory_usage_ratio"),
        ("circuit_breakers_open", "circuit_breaker_open_sources"),
        ("dead_letters", "job_dead_letters_unresolved"),
        ("api_up", "up{job='data-core-api'}"),
    ],
    "poupi-crypto": [
        ("scheduler_heartbeat_crypto", "optruth_scheduler_heartbeat_age_seconds"),
        ("operational_confidence", "optruth_operational_confidence_score"),
        ("runtime_score", "optruth_runtime_truth_score"),
        ("infra_score", "optruth_infra_score"),
        ("queue_pressure", "optruth_queue_pressure_score"),
        ("enforcement_mode", "enforcement_current_mode"),
        ("redis_up", "redis_up{job='poupi-crypto-core-15m'}"),
        ("safe_mode", "optruth_safe_mode_active"),
    ],
    "poupi-baby": [
        ("active_offers", "poupi_business_health_value{check='activeOffers'}"),
        ("feed_24h", "poupi_business_health_value{check='feed24h'}"),
        ("telegram_pipeline", "poupi_business_health_status{check='telegramPipeline'}"),
        ("dataset_quality", "poupi_dataset_quality_score"),
        ("scraper_success_rate", "poupi_scraper_success_rate"),
        ("circuit_breakers", "circuit_breaker_open_sources"),
        ("backend_up", "up{job='poupi-baby-backend'}"),
        ("worker_up", "up{job='poupi-baby-worker'}"),
    ],
}

# Queries universais (sempre coletadas)
_UNIVERSAL_QUERIES: list[tuple[str, str]] = [
    ("data_core_api_up", "up{job='data-core-api'}"),
]


class MetricsCollector(BaseCollector):
    name = "metrics"
    timeout_seconds = 10.0

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        service = context.get("service", "")
        alert_id = context.get("alert_id", "")

        queries = list(_UNIVERSAL_QUERIES)
        queries += _METRIC_QUERIES.get(service, [])

        results: dict[str, Any] = {}
        errors: list[str] = []

        for metric_name, query in queries:
            try:
                value = self._instant_query(query)
                results[metric_name] = value
            except Exception as exc:
                errors.append(f"{metric_name}: {exc}")

        return {
            "service": service,
            "alert_id": alert_id,
            "metrics": results,
            "metrics_collected": len(results),
            "errors": errors,
        }

    def _instant_query(self, query: str) -> Any:
        """Executa uma instant query no Prometheus e retorna o valor."""
        params = urllib.parse.urlencode({"query": query})
        url = f"{_PROMETHEUS_URL}/api/v1/query?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = json.loads(resp.read(16384).decode("utf-8"))

        if body.get("status") != "success":
            raise ValueError(f"Prometheus error: {body.get('error', 'unknown')}")

        result_data = body.get("data", {}).get("result", [])
        if not result_data:
            return None

        # Para instant queries, retorna o valor mais recente
        if len(result_data) == 1:
            _, value = result_data[0]["value"]
            try:
                return float(value)
            except (TypeError, ValueError):
                return value

        # Para múltiplos resultados (por label), retorna dict
        return {
            str(r.get("metric", {})): float(r["value"][1])
            for r in result_data
            if "value" in r
        }

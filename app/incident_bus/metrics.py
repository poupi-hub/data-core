from prometheus_client import Counter

INCIDENT_RECEIVED = Counter(
    "incident_bus_events_received_total",
    "Total de alertas recebidos via webhook do Alertmanager",
    ["severity", "status"],
)

INCIDENT_PERSISTED = Counter(
    "incident_bus_events_persisted_total",
    "Total de alertas persistidos com sucesso na tabela incident_events",
    ["severity", "alert_id"],
)

INCIDENT_ERRORS = Counter(
    "incident_bus_errors_total",
    "Total de erros ao processar/persistir alertas no incident bus",
    ["alertname"],
)

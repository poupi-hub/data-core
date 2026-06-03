from prometheus_client import Counter, Histogram

HISTORY_RECORDS_CREATED = Counter(
    "incident_history_records_created_total",
    "Total de registros criados em incident_history",
)

HISTORY_PATTERNS_UPDATED = Counter(
    "incident_history_patterns_updated_total",
    "Total de incident_patterns re-agregados",
)

HISTORY_AGGREGATION_ERRORS = Counter(
    "incident_history_aggregation_errors_total",
    "Total de erros durante a agregação do histórico",
)

HISTORY_AGGREGATION_DURATION = Histogram(
    "incident_history_aggregation_duration_seconds",
    "Duração do job de agregação de histórico",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

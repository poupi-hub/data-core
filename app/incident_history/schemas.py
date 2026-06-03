from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RootCauseBucket(BaseModel):
    bucket: str
    count: int
    pct: float


class IncidentHistoryCreate(BaseModel):
    incident_event_id: int | None = None
    alert_id:  str | None = None
    alertname: str
    service:   str | None = None
    severity:  str
    category:  str | None = None

    root_cause:        str | None = None
    root_cause_bucket: str | None = None
    rca_confidence:    float | None = None

    resolution:      str | None = None
    resolution_type: str | None = None
    resolved_by:     str | None = None

    fired_at:         datetime | None = None
    resolved_at:      datetime | None = None
    duration_seconds: int | None = None

    ai_action_used:   str | None = None
    runbook:          str | None = None
    context_snapshot: dict[str, Any] | None = None


class IncidentHistoryRead(BaseModel):
    id: int
    incident_event_id: int | None
    alert_id:  str | None
    alertname: str
    service:   str | None
    severity:  str
    root_cause:        str | None
    root_cause_bucket: str | None
    rca_confidence:    float | None
    resolution:        str | None
    resolution_type:   str | None
    resolved_by:       str | None
    fired_at:          datetime | None
    resolved_at:       datetime | None
    duration_seconds:  int | None
    recorded_at:       datetime
    ai_action_used:    str | None
    runbook:           str | None

    model_config = {"from_attributes": True}


class IncidentPatternRead(BaseModel):
    alert_id:  str
    alertname: str
    service:   str | None
    severity:  str

    total_occurrences: int
    resolved_count:    int
    unresolved_count:  int
    last_fired_at:     datetime | None
    first_fired_at:    datetime | None

    mttr_seconds:     float | None
    mttr_p50_seconds: float | None
    mttr_p90_seconds: float | None

    top_root_causes: list[RootCauseBucket] | None = None
    recurrence_interval_hours: float | None
    is_flapping:               bool
    rca_confidence_avg:        float | None
    last_aggregated_at:        datetime

    model_config = {"from_attributes": True}


class HistoryList(BaseModel):
    total: int
    items: list[IncidentHistoryRead]


class PatternList(BaseModel):
    total: int
    items: list[IncidentPatternRead]


class AggregationResult(BaseModel):
    """Resultado do job de agregação."""
    processed_events: int
    new_history_records: int
    updated_patterns: int
    errors: int
    duration_ms: float

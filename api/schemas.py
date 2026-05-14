from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from database.models import CollectorDomain, RunStatus


class DependencyStatus(BaseModel):
    status: str
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    app: str
    environment: str
    dependencies: dict[str, DependencyStatus] | None = None


class CollectorResponse(BaseModel):
    name: str
    domain: CollectorDomain
    source: str
    description: str
    default_interval_minutes: int
    module: str | None = None
    collector_version: str = "1.0.0"
    raw_schema_name: str = "genericJson"
    raw_schema_version: str = "1.0.0"
    registered_versions: int = 0


class RunCollectorResponse(BaseModel):
    id: UUID
    collector_name: str
    module: str | None = None
    domain: CollectorDomain | None = None
    source: str | None = None
    source_name: str | None = None
    collector_version: str | None = None
    raw_schema_name: str | None = None
    raw_schema_version: str | None = None
    status: RunStatus
    started_at: datetime | None
    finished_at: datetime | None
    items_collected: int
    raw_saved_count: int = 0
    error_count: int = 0
    error_message: str | None

    model_config = ConfigDict(from_attributes=True)


class CollectedRecordResponse(BaseModel):
    id: UUID
    collector_name: str
    domain: CollectorDomain
    source: str
    external_id: str | None
    source_url: str | None
    payload: dict[str, Any]
    collected_at: datetime

    model_config = ConfigDict(from_attributes=True)

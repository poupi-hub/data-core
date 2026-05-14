from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from api.deps import db_session
from api.schemas import CollectedRecordResponse, CollectorResponse, RunCollectorResponse
from cache import cache_get, cache_invalidate, cache_set
from collectors.registry import registry
from database.models import CollectedRecord, CollectionRun
from app.raw.models import CollectorVersion
from workers.collector_worker import run_collector_by_name

router = APIRouter(prefix="/api/v1")


@router.get("/collectors", response_model=list[CollectorResponse])
def list_collectors(db: Session = Depends(db_session)) -> list[CollectorResponse]:
    cached = cache_get("collectors:list")
    if cached is not None:
        return cached

    version_counts = {
        name: count
        for name, count in db.query(CollectorVersion.collector_name, func.count(CollectorVersion.id))
        .group_by(CollectorVersion.collector_name)
        .all()
    }
    responses = []
    for collector in registry.all():
        metadata = collector.metadata
        module = "sports_odds" if metadata.domain.value == "sports_betting" else metadata.domain.value
        responses.append(
            CollectorResponse(
                name=metadata.name,
                domain=metadata.domain,
                source=metadata.source,
                description=metadata.description,
                default_interval_minutes=metadata.default_interval_minutes,
                module=module,
                collector_version=metadata.collector_version,
                raw_schema_name=metadata.raw_schema_name,
                raw_schema_version=metadata.raw_schema_version,
                registered_versions=version_counts.get(metadata.name, 0),
            )
        )
    responses.append(
        CollectorResponse(
            name="poupi_legacy_raw_collector",
            domain="ecommerce",
            source="poupi_legacy",
            description="Temporary bridge for Poupi Baby legacy TypeScript scrapers.",
            default_interval_minutes=0,
            module="ecommerce",
            collector_version="1.0.0",
            raw_schema_name="scrapedProduct",
            raw_schema_version="1.0.0",
            registered_versions=version_counts.get("poupi_legacy_raw_collector", 0),
        )
    )
    cache_set("collectors:list", [r.model_dump() for r in responses], ttl_seconds=300)
    return responses


@router.post(
    "/collectors/{collector_name}/run",
    response_model=RunCollectorResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_collector(collector_name: str, db: Session = Depends(db_session)) -> CollectionRun:
    if collector_name not in registry.names():
        raise HTTPException(status_code=404, detail="Collector not found")
    cache_invalidate("collectors:*")
    return await run_collector_by_name(collector_name, db)


@router.get("/runs", response_model=list[RunCollectorResponse])
def list_runs(
    db: Session = Depends(db_session),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[CollectionRun]:
    return (
        db.query(CollectionRun)
        .order_by(desc(CollectionRun.created_at))
        .limit(limit)
        .all()
    )


@router.get("/records", response_model=list[CollectedRecordResponse])
def list_records(
    db: Session = Depends(db_session),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[CollectedRecord]:
    return (
        db.query(CollectedRecord)
        .order_by(desc(CollectedRecord.collected_at))
        .limit(limit)
        .all()
    )

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from api.deps import db_session
from app.analytics.models import (
    CryptoAnalytics,
    ProductPriceAnalytics,
    RealEstateAnalytics,
    SportsOddsAnalytics,
    TradingAnalytics,
)
from app.normalization.models import (
    NormalizedCryptoSnapshot,
    NormalizedMarketCandle,
    NormalizedProduct,
    NormalizedRealEstateListing,
    NormalizedSportsOdd,
    NormalizerVersion,
)
from app.raw.models import CollectorVersion, RawCollection
from app.raw.repository import RawRepository
from app.data_quality.models import DataQualityRun
from app.documentation.models import DataLineage, DataSla
from app.documentation.services import DocumentationService
from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus
from scheduler.jobs import MODULE_COLLECTORS, ensure_default_collection_targets, run_collection_targets_job

router = APIRouter(prefix="/api/v1", tags=["pipeline"])

NORMALIZED_TABLES = {
    "ecommerce": NormalizedProduct,
    "real_estate": NormalizedRealEstateListing,
    "crypto": NormalizedCryptoSnapshot,
    "trading": NormalizedMarketCandle,
    "sports_odds": NormalizedSportsOdd,
}

ANALYTICS_TABLES = {
    "ecommerce": ProductPriceAnalytics,
    "real_estate": RealEstateAnalytics,
    "crypto": CryptoAnalytics,
    "trading": TradingAnalytics,
    "sports_odds": SportsOddsAnalytics,
}


class ResolveCollectorErrorRequest(BaseModel):
    resolution_note: str | None = None


class CollectionTargetRequest(BaseModel):
    module: str
    source_name: str
    collector_name: str
    target_url: str
    active: bool = True
    metadata_json: dict[str, Any] | None = None


class CollectionTargetUpdateRequest(BaseModel):
    module: str | None = None
    source_name: str | None = None
    collector_name: str | None = None
    target_url: str | None = None
    active: bool | None = None
    metadata_json: dict[str, Any] | None = None


class CollectionTargetImportRequest(BaseModel):
    targets: list[CollectionTargetRequest]
    default_metadata_json: dict[str, Any] | None = None

@router.get("/raw-collections")
def list_raw_collections(
    db: Session = Depends(db_session),
    module: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    rows = RawRepository(db).list_rows(module=module, status=status, limit=limit, offset=offset)
    return [_to_dict(row, exclude={"raw_content", "raw_json"}) for row in rows]


@router.get("/raw-collections/{raw_id}")
def get_raw_collection(raw_id: UUID, db: Session = Depends(db_session)) -> dict[str, Any]:
    raw = RawRepository(db).get(str(raw_id))
    if not raw:
        raise HTTPException(status_code=404, detail="RAW collection not found")
    return _to_dict(raw)


@router.get("/collection-runs")
def list_collection_runs(
    db: Session = Depends(db_session),
    module: str | None = None,
    collector_name: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    query = db.query(CollectionRun)
    if module:
        query = query.filter(CollectionRun.module == module)
    if collector_name:
        query = query.filter(CollectionRun.collector_name == collector_name)
    rows = query.order_by(desc(CollectionRun.created_at)).offset(offset).limit(limit).all()
    return [_to_dict(row) for row in rows]


@router.get("/collection-targets")
def list_collection_targets(
    db: Session = Depends(db_session),
    module: str | None = None,
    source_name: str | None = None,
    collector_name: str | None = None,
    active: bool | None = True,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    ensure_default_collection_targets()
    query = db.query(CollectionTarget)
    if module:
        query = query.filter(CollectionTarget.module == module)
    if source_name:
        query = query.filter(CollectionTarget.source_name == source_name)
    if collector_name:
        query = query.filter(CollectionTarget.collector_name == collector_name)
    if active is not None:
        query = query.filter(CollectionTarget.active.is_(active))
    rows = query.order_by(CollectionTarget.module, CollectionTarget.source_name, CollectionTarget.created_at).offset(offset).limit(limit).all()
    return [_to_dict(row) for row in rows]


@router.post("/collection-targets")
def upsert_collection_target(
    payload: CollectionTargetRequest,
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    target, _created = _upsert_collection_target(db, payload)
    db.commit()
    db.refresh(target)
    return _to_dict(target)


@router.post("/collection-targets/import")
def import_collection_targets(
    payload: CollectionTargetImportRequest,
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    created = 0
    updated = 0
    rows = []
    for target_payload in payload.targets:
        merged_metadata = {
            **(payload.default_metadata_json or {}),
            **(target_payload.metadata_json or {}),
        }
        target, was_created = _upsert_collection_target(
            db,
            target_payload.model_copy(update={"metadata_json": merged_metadata}),
        )
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
        rows.append(target)
    db.commit()
    return {
        "created": created,
        "updated": updated,
        "total": len(rows),
        "targets": [_to_dict(row) for row in rows],
    }


@router.patch("/collection-targets/{target_id}")
def update_collection_target(
    target_id: UUID,
    payload: CollectionTargetUpdateRequest,
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    target = db.get(CollectionTarget, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Collection target not found")
    for field in ("module", "source_name", "collector_name", "target_url", "active", "metadata_json"):
        value = getattr(payload, field)
        if value is not None:
            setattr(target, field, value)
    db.commit()
    db.refresh(target)
    return _to_dict(target)


@router.get("/collection-targets/{target_id}/status")
def collection_target_status(target_id: UUID, db: Session = Depends(db_session)) -> dict[str, Any]:
    target = db.get(CollectionTarget, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Collection target not found")
    latest_run = (
        db.query(CollectionRun)
        .filter(
            CollectionRun.module == target.module,
            CollectionRun.collector_name == target.collector_name,
            CollectionRun.source_name.in_([target.source_name, "poupi_legacy"]),
        )
        .order_by(desc(CollectionRun.started_at), desc(CollectionRun.created_at))
        .first()
    )
    latest_raw = (
        db.query(RawCollection)
        .filter(
            RawCollection.module == target.module,
            RawCollection.source_name == target.source_name,
            RawCollection.collector_name == target.collector_name,
            RawCollection.target_url == target.target_url,
        )
        .order_by(desc(RawCollection.collected_at))
        .first()
    )
    normalized = []
    analytics = []
    if target.module == "ecommerce" and latest_raw:
        normalized = (
            db.query(NormalizedProduct)
            .filter(NormalizedProduct.raw_collection_id == latest_raw.id)
            .order_by(desc(NormalizedProduct.normalized_at), desc(NormalizedProduct.collected_at))
            .all()
        )
        product_ids = [item.id for item in normalized]
        if product_ids:
            analytics = (
                db.query(ProductPriceAnalytics)
                .filter(ProductPriceAnalytics.product_id.in_(product_ids))
                .order_by(desc(ProductPriceAnalytics.calculated_at))
                .all()
            )
    freshness = _freshness_entry(
        module=target.module,
        source_name=target.source_name,
        latest_collected_at=latest_raw.collected_at if latest_raw else None,
        latest_run_at=latest_run.finished_at if latest_run and latest_run.status == RunStatus.success else None,
        raw_count=1 if latest_raw else 0,
        freshness_sla=_freshness_sla_for(db, target.module, target.source_name),
        now=datetime.now(timezone.utc),
    )
    return {
        "target": _to_dict(target),
        "freshness": freshness,
        "latest_run": _to_dict(latest_run) if latest_run else None,
        "latest_raw": _to_dict(latest_raw, exclude={"raw_content", "raw_json"}) if latest_raw else None,
        "normalized": [_to_dict(row) for row in normalized],
        "analytics": [_to_dict(row) for row in analytics],
    }


@router.delete("/collection-targets/{target_id}")
def disable_collection_target(target_id: UUID, db: Session = Depends(db_session)) -> dict[str, Any]:
    target = db.get(CollectionTarget, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Collection target not found")
    target.active = False
    db.commit()
    db.refresh(target)
    return _to_dict(target)


@router.post("/collection-targets/run")
def run_collection_targets(
    module: str | None = None,
    source_name: str | None = None,
    collector_name: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, int]:
    return run_collection_targets_job(module=module, source=source_name, collector_name=collector_name, limit=limit)


@router.get("/sources/{module}/{source_name}/status")
def source_status(module: str, source_name: str, db: Session = Depends(db_session)) -> dict[str, Any]:
    module = _canonical_module(module) or module
    target_rows = (
        db.query(CollectionTarget)
        .filter(CollectionTarget.module == module, CollectionTarget.source_name == source_name)
        .order_by(CollectionTarget.active.desc(), CollectionTarget.created_at)
        .all()
    )
    collector_names = sorted({target.collector_name for target in target_rows})
    run_source_names = [source_name]
    if module == "ecommerce" and any(target.collector_name == "poupi_legacy_raw_collector" for target in target_rows):
        run_source_names.append("poupi_legacy")
    latest_run = (
        db.query(CollectionRun)
        .filter(CollectionRun.module == module, CollectionRun.source_name.in_(run_source_names))
        .order_by(desc(CollectionRun.started_at), desc(CollectionRun.created_at))
        .first()
    )
    latest_raw = (
        db.query(RawCollection)
        .filter(RawCollection.module == module, RawCollection.source_name == source_name)
        .order_by(desc(RawCollection.collected_at))
        .first()
    )
    raw_status_counts = {
        status: count
        for status, count in (
            db.query(RawCollection.processing_status, func.count(RawCollection.id))
            .filter(RawCollection.module == module, RawCollection.source_name == source_name)
            .group_by(RawCollection.processing_status)
            .all()
        )
    }
    normalized_count = _normalized_count_for_source(db, module, source_name)
    analytics_pending = _analytics_pending_for_source(db, module, source_name)
    unresolved_errors_query = db.query(CollectorError).filter(CollectorError.resolved_at.is_(None))
    if collector_names:
        unresolved_errors_query = unresolved_errors_query.filter(CollectorError.collector_name.in_(collector_names))
    freshness = _freshness_entry(
        module=module,
        source_name=source_name,
        latest_collected_at=latest_raw.collected_at if latest_raw else None,
        latest_run_at=latest_run.finished_at if latest_run and latest_run.status == RunStatus.success else None,
        raw_count=sum(raw_status_counts.values()),
        freshness_sla=_freshness_sla_for(db, module, source_name),
        now=datetime.now(timezone.utc),
    )
    return {
        "module": module,
        "source_name": source_name,
        "freshness": freshness,
        "targets": {
            "total": len(target_rows),
            "active": sum(1 for target in target_rows if target.active),
            "items": [_to_dict(target) for target in target_rows],
        },
        "latest_run": _to_dict(latest_run) if latest_run else None,
        "latest_raw": _to_dict(latest_raw, exclude={"raw_content", "raw_json"}) if latest_raw else None,
        "raw_status_counts": raw_status_counts,
        "normalized_count": normalized_count,
        "analytics_pending": analytics_pending,
        "unresolved_collector_errors": [_to_dict(row) for row in unresolved_errors_query.order_by(desc(CollectorError.created_at)).limit(10).all()],
    }


@router.get("/sources/ecommerce/{source_name}/price-changes")
def ecommerce_price_changes(
    source_name: str,
    db: Session = Depends(db_session),
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=500),
    include_unchanged: bool = False,
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(NormalizedProduct)
        .filter(
            NormalizedProduct.store_name == source_name,
            NormalizedProduct.price.is_not(None),
            NormalizedProduct.collected_at >= since,
        )
        .order_by(NormalizedProduct.store_name, NormalizedProduct.external_id, desc(NormalizedProduct.collected_at))
        .limit(limit * 20)
        .all()
    )
    groups: dict[str, list[NormalizedProduct]] = {}
    for row in rows:
        key = row.external_id or row.source_id or row.title or str(row.id)
        groups.setdefault(key, []).append(row)

    changes = []
    for key, snapshots in groups.items():
        ordered = sorted(snapshots, key=lambda item: item.collected_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        latest = ordered[0]
        previous = _previous_distinct_price_snapshot(ordered)
        current_price = _decimal_or_none(latest.price)
        previous_price = _decimal_or_none(previous.price) if previous else None
        if previous_price is None and not include_unchanged:
            continue
        changed = previous_price is not None and current_price != previous_price
        if not changed and not include_unchanged:
            continue
        delta = current_price - previous_price if current_price is not None and previous_price is not None else None
        change_percent = (
            (delta / previous_price * Decimal("100")).quantize(Decimal("0.01"))
            if delta is not None and previous_price not in (None, Decimal("0"))
            else None
        )
        changes.append(
            {
                "identity": key,
                "product_id": str(latest.id),
                "raw_collection_id": str(latest.raw_collection_id),
                "title": latest.title,
                "store_name": latest.store_name,
                "target_url": (latest.normalization_metadata_json or {}).get("target_url"),
                "current_price": str(current_price) if current_price is not None else None,
                "previous_price": str(previous_price) if previous_price is not None else None,
                "delta": str(delta) if delta is not None else None,
                "change_percent": str(change_percent) if change_percent is not None else None,
                "direction": _price_change_direction(delta),
                "current_collected_at": latest.collected_at,
                "previous_collected_at": previous.collected_at if previous else None,
                "snapshot_count": len(ordered),
            }
        )
    changes.sort(key=lambda item: (item["current_collected_at"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return {
        "module": "ecommerce",
        "source_name": source_name,
        "days": days,
        "count": min(len(changes), limit),
        "items": changes[:limit],
        "semantics": {
            "history": "normalized_products snapshots",
            "duplicate_policy": "consecutive equal prices are skipped when finding previous_price",
            "direction": "down means current_price is lower than previous_price",
        },
    }


@router.get("/jobs/status")
def jobs_status(db: Session = Depends(db_session)) -> dict[str, Any]:
    latest_runs = (
        db.query(CollectionRun)
        .order_by(desc(CollectionRun.started_at), desc(CollectionRun.created_at))
        .limit(25)
        .all()
    )
    return {
        "modules": [
            {"module": module, "collectors": collectors}
            for module, collectors in sorted(MODULE_COLLECTORS.items())
        ],
        "latest_runs": [_to_dict(run) for run in latest_runs],
    }


@router.get("/collectors/{collector_name}/versions")
def list_collector_versions(collector_name: str, db: Session = Depends(db_session)) -> list[dict[str, Any]]:
    rows = (
        db.query(CollectorVersion)
        .filter(CollectorVersion.collector_name == collector_name)
        .order_by(desc(CollectorVersion.created_at))
        .all()
    )
    return [_to_dict(row) for row in rows]


@router.get("/normalizers")
def list_normalizers(db: Session = Depends(db_session)) -> list[dict[str, Any]]:
    rows = db.query(NormalizerVersion).order_by(NormalizerVersion.module, NormalizerVersion.normalizer_name).all()
    return [_to_dict(row) for row in rows]


@router.get("/normalizers/{normalizer_name}/versions")
def list_normalizer_versions(normalizer_name: str, db: Session = Depends(db_session)) -> list[dict[str, Any]]:
    rows = (
        db.query(NormalizerVersion)
        .filter(NormalizerVersion.normalizer_name == normalizer_name)
        .order_by(desc(NormalizerVersion.created_at))
        .all()
    )
    return [_to_dict(row) for row in rows]


@router.get("/data-quality/summary")
def data_quality_summary(
    db: Session = Depends(db_session),
    normalizer_version: str | None = None,
) -> list[dict[str, Any]]:
    query = db.query(
        DataQualityRun.module,
        DataQualityRun.normalizer_name,
        DataQualityRun.normalizer_version,
        func.sum(DataQualityRun.checked_count),
        func.sum(DataQualityRun.passed_count),
        func.sum(DataQualityRun.failed_count),
    )
    if normalizer_version:
        query = query.filter(DataQualityRun.normalizer_version == normalizer_version)
    rows = query.group_by(
        DataQualityRun.module,
        DataQualityRun.normalizer_name,
        DataQualityRun.normalizer_version,
    ).all()
    return [
        {
            "module": module,
            "normalizer_name": name,
            "normalizer_version": version,
            "checked_count": int(checked or 0),
            "passed_count": int(passed or 0),
            "failed_count": int(failed or 0),
        }
        for module, name, version, checked, passed, failed in rows
    ]


@router.get("/normalized/{module}")
def list_normalized(
    module: str,
    db: Session = Depends(db_session),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    model = NORMALIZED_TABLES.get(module)
    if model is None:
        raise HTTPException(status_code=404, detail="Normalized module not found")
    rows = db.query(model).offset(offset).limit(limit).all()
    return [_to_dict(row) for row in rows]


@router.get("/analytics/{module}")
def list_analytics(
    module: str,
    db: Session = Depends(db_session),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    model = ANALYTICS_TABLES.get(module)
    if model is None:
        raise HTTPException(status_code=404, detail="Analytics module not found")
    rows = db.query(model).offset(offset).limit(limit).all()
    return [_to_dict(row) for row in rows]


@router.get("/pipeline/status")
def pipeline_status(db: Session = Depends(db_session)) -> dict[str, Any]:
    raw_statuses = (
        db.query(RawCollection.module, RawCollection.processing_status, func.count(RawCollection.id))
        .group_by(RawCollection.module, RawCollection.processing_status)
        .all()
    )
    normalized_counts = {
        module: db.query(model).count()
        for module, model in NORMALIZED_TABLES.items()
    }
    analytics_counts = {
        module: db.query(model).count()
        for module, model in ANALYTICS_TABLES.items()
    }
    return {
        "raw": [
            {"module": module, "processing_status": status, "count": count}
            for module, status, count in raw_statuses
        ],
        "normalized": normalized_counts,
        "analytics": analytics_counts,
        "supported_modules": sorted(NORMALIZED_TABLES.keys()),
    }


@router.get("/operations/summary")
def operations_summary(db: Session = Depends(db_session)) -> dict[str, Any]:
    raw_pending = (
        db.query(RawCollection.module, func.count(RawCollection.id))
        .filter(RawCollection.processing_status == "normalization_pending")
        .group_by(RawCollection.module)
        .all()
    )
    raw_failed = (
        db.query(RawCollection.module, func.count(RawCollection.id))
        .filter(RawCollection.processing_status == "normalization_failed")
        .group_by(RawCollection.module)
        .all()
    )
    analytics_pending = {
        module: db.query(model).filter(model.analytics_status == "pending").count()
        for module, model in NORMALIZED_TABLES.items()
        if hasattr(model, "analytics_status")
    }
    latest_quality = (
        db.query(DataQualityRun)
        .order_by(desc(DataQualityRun.created_at))
        .limit(10)
        .all()
    )
    recent_errors = (
        db.query(CollectorError)
        .filter(CollectorError.resolved_at.is_(None))
        .order_by(desc(CollectorError.created_at))
        .limit(10)
        .all()
    )
    return {
        "raw_pending_by_module": {module: count for module, count in raw_pending},
        "raw_failed_by_module": {module: count for module, count in raw_failed},
        "analytics_pending_by_module": analytics_pending,
        "latest_quality_runs": [_to_dict(row) for row in latest_quality],
        "recent_collector_errors": [_to_dict(row) for row in recent_errors],
    }


@router.get("/operations/latest-collections")
def latest_collections(
    db: Session = Depends(db_session),
    module: str | None = None,
    limit: int = Query(default=25, ge=1, le=200),
) -> list[dict[str, Any]]:
    query = db.query(CollectionRun)
    if module:
        query = query.filter(CollectionRun.module == module)
    rows = query.order_by(desc(CollectionRun.started_at), desc(CollectionRun.created_at)).limit(limit).all()
    return [_to_dict(row) for row in rows]


@router.get("/operations/raw-pending")
def raw_pending(
    db: Session = Depends(db_session),
    module: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    query = db.query(RawCollection).filter(RawCollection.processing_status == "normalization_pending")
    if module:
        query = query.filter(RawCollection.module == module)
    rows = query.order_by(desc(RawCollection.collected_at)).limit(limit).all()
    return [_to_dict(row, exclude={"raw_content", "raw_json"}) for row in rows]


@router.get("/operations/normalization-failures")
def normalization_failures(
    db: Session = Depends(db_session),
    module: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    query = db.query(RawCollection).filter(RawCollection.processing_status == "normalization_failed")
    if module:
        query = query.filter(RawCollection.module == module)
    rows = query.order_by(desc(RawCollection.collected_at)).limit(limit).all()
    return [_to_dict(row, exclude={"raw_content", "raw_json"}) for row in rows]


@router.get("/operations/analytics-pending")
def analytics_pending(
    module: str,
    db: Session = Depends(db_session),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    model = NORMALIZED_TABLES.get(module)
    if model is None:
        raise HTTPException(status_code=404, detail="Normalized module not found")
    if not hasattr(model, "analytics_status"):
        return []
    rows = db.query(model).filter(model.analytics_status == "pending").limit(limit).all()
    return [_to_dict(row) for row in rows]


@router.get("/operations/collector-errors")
def collector_errors(
    db: Session = Depends(db_session),
    collector_name: str | None = None,
    include_resolved: bool = False,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    query = db.query(CollectorError)
    if collector_name:
        query = query.filter(CollectorError.collector_name == collector_name)
    if not include_resolved:
        query = query.filter(CollectorError.resolved_at.is_(None))
    rows = query.order_by(desc(CollectorError.created_at)).limit(limit).all()
    return [_to_dict(row) for row in rows]


@router.post("/operations/collector-errors/{error_id}/resolve")
def resolve_collector_error(
    error_id: UUID,
    payload: ResolveCollectorErrorRequest,
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    error = db.query(CollectorError).filter(CollectorError.id == error_id).one_or_none()
    if not error:
        raise HTTPException(status_code=404, detail="Collector error not found")
    error.resolved_at = datetime.now(timezone.utc)
    error.resolution_note = payload.resolution_note or "resolved"
    db.commit()
    db.refresh(error)
    return _to_dict(error)


@router.get("/operations/freshness")
def operations_freshness(
    db: Session = Depends(db_session),
    module: str | None = None,
    include_tests: bool = False,
) -> dict[str, Any]:
    DocumentationService(db).ensure_governance_defaults()
    db.commit()
    latest_query = db.query(
        RawCollection.module,
        RawCollection.source_name,
        func.max(RawCollection.collected_at).label("latest_collected_at"),
        func.count(RawCollection.id).label("raw_count"),
    ).group_by(RawCollection.module, RawCollection.source_name)
    if module:
        latest_query = latest_query.filter(RawCollection.module == module)

    latest_rows = latest_query.all()
    latest_runs = {
        (_canonical_module(module_name), source_name): finished_at
        for module_name, source_name, finished_at in (
            db.query(
                CollectionRun.module,
                CollectionRun.source_name,
                func.max(CollectionRun.finished_at).label("latest_run_at"),
            )
            .filter(CollectionRun.status == RunStatus.success, CollectionRun.finished_at.is_not(None))
            .group_by(CollectionRun.module, CollectionRun.source_name)
            .all()
        )
    }
    sla_rows = db.query(DataSla).filter(DataSla.is_active.is_(True)).all()
    sla_by_key = {(sla.module, sla.source_name): sla for sla in sla_rows}
    module_sla = {(sla.module, None): sla for sla in sla_rows if sla.source_name is None}
    now = datetime.now(timezone.utc)

    entries = []
    seen_keys: set[tuple[str, str | None]] = set()
    for module_name, source_name, latest_collected_at, raw_count in latest_rows:
        module_name = _canonical_module(module_name)
        if _is_test_source(source_name) and not include_tests:
            continue
        key = (module_name, source_name)
        seen_keys.add(key)
        sla = sla_by_key.get(key) or module_sla.get((module_name, None))
        entries.append(
            _freshness_entry(
                module=module_name,
                source_name=source_name,
                latest_collected_at=latest_collected_at,
                latest_run_at=latest_runs.get(key),
                raw_count=int(raw_count or 0),
                freshness_sla=sla.freshness_sla if sla else None,
                now=now,
            )
        )
    for key, latest_run_at in latest_runs.items():
        if key in seen_keys:
            continue
        module_name, source_name = key
        if module and module_name != module:
            continue
        if _is_test_source(source_name) and not include_tests:
            continue
        seen_keys.add(key)
        sla = sla_by_key.get(key) or module_sla.get((module_name, None))
        entries.append(
            _freshness_entry(
                module=module_name,
                source_name=source_name,
                latest_collected_at=None,
                latest_run_at=latest_run_at,
                raw_count=0,
                freshness_sla=sla.freshness_sla if sla else None,
                now=now,
            )
        )

    for sla in sla_rows:
        key = (sla.module, sla.source_name)
        if module and sla.module != module:
            continue
        if _is_test_source(sla.source_name) and not include_tests:
            continue
        if sla.source_name is None or key in seen_keys:
            continue
        entries.append(
            _freshness_entry(
                module=sla.module,
                source_name=sla.source_name,
                latest_collected_at=None,
                latest_run_at=None,
                raw_count=0,
                freshness_sla=sla.freshness_sla,
                now=now,
            )
        )

    status_order = {"violated": 0, "warning": 1, "missing_data": 2, "unknown_sla": 3, "ok": 4}
    entries.sort(key=lambda item: (status_order.get(item["status"], 9), item["module"], item["source_name"] or ""))
    return {
        "generated_at": now,
        "items": entries,
        "summary": {
            status: sum(1 for item in entries if item["status"] == status)
            for status in ["ok", "warning", "violated", "missing_data", "unknown_sla"]
        },
    }


@router.get("/lineage/products/{product_id}")
def product_lineage(product_id: UUID, db: Session = Depends(db_session)) -> dict[str, Any]:
    product = db.get(NormalizedProduct, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    raw = db.get(RawCollection, product.raw_collection_id)
    analytics = (
        db.query(ProductPriceAnalytics)
        .filter(ProductPriceAnalytics.product_id == product.id)
        .order_by(desc(ProductPriceAnalytics.calculated_at))
        .all()
    )
    lineage_rows = (
        db.query(DataLineage)
        .filter(DataLineage.normalized_record_type == NormalizedProduct.__tablename__)
        .filter(DataLineage.normalized_record_id == product.id)
        .order_by(DataLineage.created_at)
        .all()
    )
    return {
        "product": _to_dict(product),
        "raw_collection": _to_dict(raw, exclude={"raw_content", "raw_json"}) if raw else None,
        "normalization": {
            "normalizer_name": product.normalizer_name,
            "normalizer_version": product.normalizer_version,
            "normalized_at": product.normalized_at,
            "source_raw_schema_name": product.source_raw_schema_name,
            "source_raw_schema_version": product.source_raw_schema_version,
            "source_collector_name": product.source_collector_name,
            "source_collector_version": product.source_collector_version,
        },
        "analytics": [_to_dict(row) for row in analytics],
        "lineage": [_to_dict(row) for row in lineage_rows],
    }


def _to_dict(row: object, *, exclude: set[str] | None = None) -> dict[str, Any]:
    exclude = exclude or set()
    data: dict[str, Any] = {}
    for column in row.__table__.columns:
        if column.name in exclude:
            continue
        attr_name = "metadata_" if column.name == "metadata" and hasattr(row, "metadata_") else column.name
        data[column.name] = getattr(row, attr_name)
    return data


def _upsert_collection_target(db: Session, payload: CollectionTargetRequest) -> tuple[CollectionTarget, bool]:
    target = (
        db.query(CollectionTarget)
        .filter(
            CollectionTarget.module == payload.module,
            CollectionTarget.source_name == payload.source_name,
            CollectionTarget.collector_name == payload.collector_name,
            CollectionTarget.target_url == payload.target_url,
        )
        .one_or_none()
    )
    created = target is None
    if target is None:
        target = CollectionTarget(
            module=payload.module,
            source_name=payload.source_name,
            collector_name=payload.collector_name,
            target_url=payload.target_url,
            active=payload.active,
            metadata_json=payload.metadata_json or {},
        )
        db.add(target)
        db.flush()
        return target, True
    target.active = payload.active
    target.metadata_json = payload.metadata_json or target.metadata_json or {}
    db.flush()
    return target, created


def _freshness_entry(
    *,
    module: str,
    source_name: str | None,
    latest_collected_at: datetime | None,
    latest_run_at: datetime | None,
    raw_count: int,
    freshness_sla: str | None,
    now: datetime,
) -> dict[str, Any]:
    sla_delta = _parse_freshness_sla(freshness_sla)
    if latest_collected_at and latest_collected_at.tzinfo is None:
        latest_collected_at = latest_collected_at.replace(tzinfo=timezone.utc)
    if latest_run_at and latest_run_at.tzinfo is None:
        latest_run_at = latest_run_at.replace(tzinfo=timezone.utc)
    freshness_at = max([value for value in [latest_collected_at, latest_run_at] if value], default=None)
    age_seconds = int((now - freshness_at).total_seconds()) if freshness_at else None
    if not freshness_at:
        status = "missing_data"
    elif not sla_delta:
        status = "unknown_sla"
    elif age_seconds is not None and age_seconds <= int(sla_delta.total_seconds()):
        status = "ok"
    elif age_seconds is not None and age_seconds <= int(sla_delta.total_seconds() * 1.5):
        status = "warning"
    else:
        status = "violated"
    return {
        "module": module,
        "source_name": source_name,
        "latest_collected_at": latest_collected_at,
        "latest_run_at": latest_run_at,
        "freshness_at": freshness_at,
        "age_seconds": age_seconds,
        "freshness_sla": freshness_sla or "not_defined",
        "freshness_sla_seconds": int(sla_delta.total_seconds()) if sla_delta else None,
        "raw_count": raw_count,
        "status": status,
    }


def _parse_freshness_sla(value: str | None) -> timedelta | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"not_defined", "none", "na", "n/a"}:
        return None
    aliases = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
    }
    if normalized in aliases:
        return aliases[normalized]
    match = re.fullmatch(r"(\d+)\s*(m|min|minute|minutes|h|hour|hours|d|day|days)", normalized)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit in {"m", "min", "minute", "minutes"}:
        return timedelta(minutes=amount)
    if unit in {"h", "hour", "hours"}:
        return timedelta(hours=amount)
    return timedelta(days=amount)


def _freshness_sla_for(db: Session, module: str, source_name: str | None) -> str | None:
    source_sla = (
        db.query(DataSla)
        .filter(DataSla.module == module, DataSla.source_name == source_name, DataSla.is_active.is_(True))
        .one_or_none()
    )
    if source_sla:
        return source_sla.freshness_sla
    module_sla = (
        db.query(DataSla)
        .filter(DataSla.module == module, DataSla.source_name.is_(None), DataSla.is_active.is_(True))
        .one_or_none()
    )
    return module_sla.freshness_sla if module_sla else None


def _normalized_count_for_source(db: Session, module: str, source_name: str) -> int:
    model = NORMALIZED_TABLES.get(module)
    if model is None:
        return 0
    if model is NormalizedProduct:
        return db.query(model).filter(model.store_name == source_name).count()
    raw_ids = db.query(RawCollection.id).filter(RawCollection.module == module, RawCollection.source_name == source_name)
    return db.query(model).filter(model.raw_collection_id.in_(raw_ids)).count()


def _analytics_pending_for_source(db: Session, module: str, source_name: str) -> int:
    model = NORMALIZED_TABLES.get(module)
    if model is None or not hasattr(model, "analytics_status"):
        return 0
    query = db.query(model).filter(model.analytics_status == "pending")
    if model is NormalizedProduct:
        query = query.filter(model.store_name == source_name)
    else:
        raw_ids = db.query(RawCollection.id).filter(RawCollection.module == module, RawCollection.source_name == source_name)
        query = query.filter(model.raw_collection_id.in_(raw_ids))
    return query.count()


def _previous_distinct_price_snapshot(snapshots: list[NormalizedProduct]) -> NormalizedProduct | None:
    if not snapshots:
        return None
    latest_price = _decimal_or_none(snapshots[0].price)
    for snapshot in snapshots[1:]:
        price = _decimal_or_none(snapshot.price)
        if price is not None and price != latest_price:
            return snapshot
    return snapshots[1] if len(snapshots) > 1 else None


def _decimal_or_none(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _price_change_direction(delta: Decimal | None) -> str:
    if delta is None or delta == 0:
        return "unchanged"
    return "up" if delta > 0 else "down"


def _canonical_module(module: str | None) -> str | None:
    return "sports_odds" if module == "sports_betting" else module


def _is_test_source(source_name: str | None) -> bool:
    if not source_name:
        return False
    lowered = source_name.lower()
    return lowered.startswith("pytest-") or lowered.endswith("-test")

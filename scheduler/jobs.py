import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, outerjoin

from app.analytics.registry import analytics_registry
from app.modules.registry import register_pipeline_modules
from app.normalization.registry import normalizer_registry
from app.raw.models import RawCollection
from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus
from database.session import SessionLocal
from workers.collector_worker import run_collector_by_name

logger = logging.getLogger(__name__)


def run_collector_job(collector_name: str) -> None:
    async def _run() -> None:
        db = SessionLocal()
        try:
            await run_collector_by_name(collector_name, db)
        finally:
            db.close()

    logger.info("Starting scheduled collector", extra={"collector": collector_name})
    asyncio.run(_run())


def collect_raw_job(collector_name: str) -> None:
    logger.info("Starting RAW collection job", extra={"collector": collector_name})
    run_collector_job(collector_name)


def normalize_job(module: str | None = None, limit: int = 100) -> None:
    register_pipeline_modules()
    modules = [module] if module else normalizer_registry.modules()
    for module_name in modules:
        for normalizer_type in normalizer_registry.all().get(module_name, []):
            logger.info(
                "Starting normalization job",
                extra={"pipeline_module": module_name, "normalizer": normalizer_type.__name__},
            )
            db = SessionLocal()
            try:
                normalizer_type(db).run(limit=limit)
            finally:
                db.close()


MODULE_COLLECTORS = {
    "ecommerce": ["ecommerce.generic_product"],
    "real_estate": ["real_estate.generic_listing"],
    "sports_odds": ["sports_betting.generic_odds"],
    "crypto": ["crypto.generic_price", "crypto.crypto_coin_ohlcv"],
    "trading": [],
}

SOURCE_COLLECTORS = {
    "generic_marketplace": "ecommerce.generic_product",
    "generic_real_estate": "real_estate.generic_listing",
    "generic_bookmaker": "sports_betting.generic_odds",
    "generic_exchange": "crypto.generic_price",
    "crypto_coin_exchange": "crypto.crypto_coin_ohlcv",
}

DEFAULT_COLLECTION_TARGETS = [
    {
        "module": "ecommerce",
        "source_name": "drogasil",
        "collector_name": "poupi_legacy_raw_collector",
        "target_url": "https://www.drogasil.com.br/fralda-pampers-confort-sec-xxxg-44-unidades-pampers-1351898.html",
        "metadata_json": {"seeded": True, "category": "baby", "kind": "validation_target"},
    }
]


def run_module_collectors_job(module: str, source: str | None = None) -> None:
    if module == "ecommerce":
        result = run_collection_targets_job(
            module="ecommerce",
            source=source,
            collector_name="poupi_legacy_raw_collector",
        )
        if result["targets"] > 0:
            logger.info("Ecommerce target collection finished", extra=result)
            return

    collectors = MODULE_COLLECTORS.get(module, [])
    if source:
        selected = SOURCE_COLLECTORS.get(source)
        collectors = [selected] if selected and selected in collectors else []
    for collector_name in collectors:
        collect_raw_job(collector_name)


def run_ecommerce_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("ecommerce", source=source)


def run_real_estate_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("real_estate", source=source)


def run_sports_odds_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("sports_odds", source=source)


def run_crypto_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("crypto", source=source)


def run_trading_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("trading", source=source)


def ensure_default_collection_targets() -> int:
    db = SessionLocal()
    created = 0
    try:
        for item in DEFAULT_COLLECTION_TARGETS:
            existing = (
                db.query(CollectionTarget)
                .filter(
                    CollectionTarget.module == item["module"],
                    CollectionTarget.source_name == item["source_name"],
                    CollectionTarget.collector_name == item["collector_name"],
                    CollectionTarget.target_url == item["target_url"],
                )
                .one_or_none()
            )
            if existing is None:
                db.add(CollectionTarget(**item))
                created += 1
        db.commit()
        return created
    finally:
        db.close()


def run_collection_targets_job(
    module: str | None = None,
    source: str | None = None,
    collector_name: str | None = None,
    limit: int = 100,
    max_targets: int | None = None,
    delay_seconds: float = 0.0,
    timeout_seconds: int | None = None,
    dry_run: bool = False,
    list_only: bool = False,
) -> dict[str, object]:
    ensure_default_collection_targets()
    db = SessionLocal()
    try:
        # Subquery: last collected_at per (source_name, collector_name, target_url)
        last_col = (
            db.query(
                RawCollection.source_name,
                RawCollection.collector_name,
                RawCollection.target_url,
                func.max(RawCollection.collected_at).label("last_collected_at"),
            )
            .group_by(RawCollection.source_name, RawCollection.collector_name, RawCollection.target_url)
            .subquery()
        )
        query = (
            db.query(CollectionTarget)
            .outerjoin(
                last_col,
                (CollectionTarget.source_name == last_col.c.source_name)
                & (CollectionTarget.collector_name == last_col.c.collector_name)
                & (CollectionTarget.target_url == last_col.c.target_url),
            )
            .filter(CollectionTarget.active.is_(True))
            .order_by(last_col.c.last_collected_at.asc().nullsfirst(), CollectionTarget.created_at)
        )
        if module:
            query = query.filter(CollectionTarget.module == module)
        if source:
            query = query.filter(CollectionTarget.source_name == source)
        if collector_name:
            query = query.filter(CollectionTarget.collector_name == collector_name)
        target_limit = max_targets if max_targets is not None else limit
        targets = query.limit(target_limit).all()
        target_rows = [
            {
                "id": str(target.id),
                "module": target.module,
                "source_name": target.source_name,
                "collector_name": target.collector_name,
                "target_url": target.target_url,
            }
            for target in targets
        ]
        if dry_run or list_only:
            return {
                "targets": len(targets),
                "raw_saved_count": 0,
                "error_count": 0,
                "skipped_locked": 0,
                "dry_run": dry_run,
                "list_only": list_only,
                "target_limit": target_limit,
                "targets_detail": target_rows,
            }
        grouped: dict[str, list[CollectionTarget]] = defaultdict(list)
        for target in targets:
            grouped[target.collector_name].append(target)

        raw_saved = 0
        errors = 0
        skipped_locked = 0
        for selected_collector_name, selected_targets in grouped.items():
            available_targets = []
            for target in selected_targets:
                if _has_running_target_lock(db, target):
                    logger.info(
                        "Collection target skipped because an active run exists",
                        extra={"collector": selected_collector_name, "source_name": target.source_name},
                    )
                    skipped_locked += 1
                else:
                    available_targets.append(target)
            if not available_targets:
                continue
            if selected_collector_name == "poupi_legacy_raw_collector":
                result = _run_poupi_legacy_targets(
                    db,
                    available_targets,
                    delay_seconds=delay_seconds,
                    timeout_seconds=timeout_seconds,
                )
                result_counts = _coerce_poupi_target_result(result)
                raw_saved += result_counts["raw_saved_count"]
                errors += result_counts["error_count"]
            else:
                logger.warning(
                    "Collection target collector is not supported by target runner",
                    extra={"collector": selected_collector_name},
                )
                errors += len(available_targets)
        return {
            "targets": len(targets),
            "raw_saved_count": raw_saved,
            "error_count": errors,
            "skipped_locked": skipped_locked,
            "dry_run": False,
            "list_only": False,
            "target_limit": target_limit,
        }
    finally:
        db.close()


def run_collection_target_by_id(
    target_id: str,
    *,
    include_inactive: bool = False,
    delay_seconds: float = 0.0,
    timeout_seconds: int | None = None,
) -> dict[str, object]:
    db = SessionLocal()
    try:
        target = db.get(CollectionTarget, target_id)
        if target is None:
            return {
                "targets": 0,
                "raw_saved_count": 0,
                "error_count": 1,
                "skipped_locked": 0,
                "error_message": "collection target not found",
            }
        if not target.active and not include_inactive:
            return {
                "targets": 1,
                "raw_saved_count": 0,
                "error_count": 1,
                "skipped_locked": 0,
                "error_message": "collection target is inactive",
            }
        if _has_running_target_lock(db, target):
            return {
                "targets": 1,
                "raw_saved_count": 0,
                "error_count": 0,
                "skipped_locked": 1,
            }
        if target.collector_name == "poupi_legacy_raw_collector":
            result = _run_poupi_legacy_targets(
                db,
                [target],
                delay_seconds=delay_seconds,
                timeout_seconds=timeout_seconds,
            )
            result_counts = _coerce_poupi_target_result(result)
            return {
                "targets": 1,
                "raw_saved_count": result_counts["raw_saved_count"],
                "error_count": result_counts["error_count"],
                "skipped_locked": 0,
            }
        return {
            "targets": 1,
            "raw_saved_count": 0,
            "error_count": 1,
            "skipped_locked": 0,
            "error_message": f"collector {target.collector_name} is not supported by target runner",
        }
    finally:
        db.close()


def run_poupi_legacy_targets_job(source: str | None = None, limit: int = 100) -> dict[str, object]:
    result = run_collection_targets_job(
        module="ecommerce",
        source=source,
        collector_name="poupi_legacy_raw_collector",
        limit=limit,
    )
    logger.info("Poupi legacy target collection finished", extra=result)
    return result


def _run_poupi_legacy_targets(
    db,
    targets: list[CollectionTarget],
    *,
    delay_seconds: float = 0.0,
    timeout_seconds: int | None = None,
) -> dict[str, int]:
    from app.modules.ecommerce.collectors.poupi_legacy_collector import LegacyPoupiTarget, PoupiLegacyRawCollector

    collector = PoupiLegacyRawCollector(
        db,
        timeout_seconds=timeout_seconds or 180,
        retry_attempts=2,
        retry_backoff_seconds=3,
        delay_seconds=delay_seconds,
    )
    result = collector.collect_targets(
        [
            LegacyPoupiTarget(
                url=target.target_url,
                source_name=target.source_name,
                metadata={
                    "collection_target_id": str(target.id),
                    **(target.metadata_json or {}),
                },
            )
            for target in targets
        ]
    )
    return {
        "raw_saved_count": int(result.get("raw_saved_count", 0)),
        "error_count": int(result.get("error_count", 0)),
    }


def _coerce_poupi_target_result(result: int | dict[str, int]) -> dict[str, int]:
    if isinstance(result, int):
        return {"raw_saved_count": result, "error_count": 0}
    return {
        "raw_saved_count": int(result.get("raw_saved_count", 0)),
        "error_count": int(result.get("error_count", 0)),
    }


def _has_running_target_lock(db, target: CollectionTarget, *, ttl_minutes: int = 30) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    return (
        db.query(CollectionRun)
        .filter(
            CollectionRun.module == target.module,
            CollectionRun.source_name.in_([target.source_name, "poupi_legacy"]),
            CollectionRun.collector_name == target.collector_name,
            CollectionRun.status == RunStatus.running,
            CollectionRun.started_at >= cutoff,
        )
        .first()
        is not None
    )


def cleanup_stale_runs_job(ttl_minutes: int = 30) -> None:
    from scheduler.circuit_breaker import check_source_circuit

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    db = SessionLocal()
    try:
        stale = (
            db.query(CollectionRun)
            .filter(CollectionRun.status == RunStatus.running, CollectionRun.started_at < cutoff)
            .all()
        )
        failed_sources: set[tuple[str, str]] = set()
        for run in stale:
            run.status = RunStatus.failed
            run.error_message = f"Forcibly terminated: stale after {ttl_minutes}min"
            run.finished_at = datetime.now(timezone.utc)
            db.add(
                CollectorError(
                    run_id=run.id,
                    collector_name=run.collector_name,
                    error_type="StaleRunTimeout",
                    message=f"Run exceeded stale TTL of {ttl_minutes} minutes without completing",
                    context={"ttl_minutes": ttl_minutes, "started_at": run.started_at.isoformat() if run.started_at else None},
                )
            )
            if run.module and run.source_name:
                failed_sources.add((run.module, run.source_name))

        if stale:
            db.commit()
            logger.warning("Marked stale runs as failed", extra={"count": len(stale)})
            for module, source_name in failed_sources:
                check_source_circuit(db, module=module, source_name=source_name)
    finally:
        db.close()


def data_retention_job(
    raw_retention_days: int = 90,
    normalized_retention_days: int = 180,
    run_retention_days: int = 60,
    lineage_retention_days: int = 180,
    error_retention_days: int = 90,
    batch_size: int = 1000,
) -> dict[str, int]:
    """Delete old processed records to control DB growth.

    Cleans up in dependency order to avoid FK violations:
      1. ProductPriceAnalytics (depends on NormalizedProduct)
      2. NormalizedProduct processed > normalized_retention_days
      3. DataLineage linked to deleted raw/normalized (> lineage_retention_days)
      4. CollectorError resolved > error_retention_days
      5. CollectionRun finished > run_retention_days
      6. RawCollection processed/ignored > raw_retention_days
    """
    from app.normalization.models import NormalizedProduct
    from app.analytics.models import ProductPriceAnalytics
    from app.documentation.models import DataLineage

    now = datetime.now(timezone.utc)
    cutoff_raw = now - timedelta(days=raw_retention_days)
    cutoff_norm = now - timedelta(days=normalized_retention_days)
    cutoff_run = now - timedelta(days=run_retention_days)
    cutoff_lineage = now - timedelta(days=lineage_retention_days)
    cutoff_error = now - timedelta(days=error_retention_days)

    db = SessionLocal()
    totals: dict[str, int] = {
        "deleted_analytics": 0,
        "deleted_normalized": 0,
        "deleted_lineage": 0,
        "deleted_errors": 0,
        "deleted_runs": 0,
        "deleted_raw": 0,
    }
    try:
        # 1. Analytics + normalized products
        old_product_ids = [
            row[0]
            for row in db.query(NormalizedProduct.id)
            .filter(
                NormalizedProduct.analytics_status == "processed",
                NormalizedProduct.collected_at < cutoff_norm,
            )
            .limit(batch_size)
            .all()
        ]
        if old_product_ids:
            totals["deleted_analytics"] = (
                db.query(ProductPriceAnalytics)
                .filter(ProductPriceAnalytics.product_id.in_(old_product_ids))
                .delete(synchronize_session=False)
            )
            totals["deleted_normalized"] = (
                db.query(NormalizedProduct)
                .filter(NormalizedProduct.id.in_(old_product_ids))
                .delete(synchronize_session=False)
            )

        # 2. DataLineage older than cutoff (created_at on DataLineage)
        totals["deleted_lineage"] = (
            db.query(DataLineage)
            .filter(DataLineage.created_at < cutoff_lineage)
            .limit(batch_size)
            .delete(synchronize_session=False)
        )

        # 3. CollectorError resolved and old
        totals["deleted_errors"] = (
            db.query(CollectorError)
            .filter(
                CollectorError.resolved_at.isnot(None),
                CollectorError.resolved_at < cutoff_error,
            )
            .limit(batch_size)
            .delete(synchronize_session=False)
        )

        # 4. CollectionRun finished and old
        totals["deleted_runs"] = (
            db.query(CollectionRun)
            .filter(
                CollectionRun.finished_at.isnot(None),
                CollectionRun.finished_at < cutoff_run,
                CollectionRun.status.in_(["success", "failed"]),
            )
            .limit(batch_size)
            .delete(synchronize_session=False)
        )

        # 5. RawCollection processed/ignored
        old_raw_ids = [
            row[0]
            for row in db.query(RawCollection.id)
            .filter(
                RawCollection.processing_status.in_(["normalized", "ignored"]),
                RawCollection.collected_at < cutoff_raw,
            )
            .limit(batch_size)
            .all()
        ]
        if old_raw_ids:
            totals["deleted_raw"] = (
                db.query(RawCollection)
                .filter(RawCollection.id.in_(old_raw_ids))
                .delete(synchronize_session=False)
            )

        db.commit()
        logger.info("Data retention cleanup complete", extra=totals)
        return totals
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def alert_webhook_job() -> None:
    from app.pipeline_api import _build_alerts_payload
    from core.config import settings
    from notifications.webhook import send_webhook

    if not settings.alert_webhook_url:
        return

    db = SessionLocal()
    try:
        payload = _build_alerts_payload(
            db=db,
            module=None,
            source_name=None,
            raw_freshness_hours=settings.alert_webhook_raw_freshness_hours,
            raw_pending_minutes=settings.alert_webhook_raw_pending_minutes,
            analytics_pending_minutes=settings.alert_webhook_analytics_pending_minutes,
            limit=100,
        )
    finally:
        db.close()

    if not payload.get("has_alerts"):
        return

    send_webhook(
        {
            "source": "data-core",
            "event": "operational_alert",
            "environment": settings.app_env,
            "summary": payload["summary"],
            "alert_count": sum(v for v in payload["summary"].values() if isinstance(v, int)),
            "details_url": f"{settings.api_host}:{settings.api_port}/api/v1/operations/alerts",
        }
    )
    logger.info("Operational alert webhook sent", extra={"summary": payload["summary"]})


def backfill_canonical_product_id_job(batch_size: int = 500) -> dict[str, int]:
    """One-time backfill: set canonical_product_id on rows that still have NULL.

    Mirrors the priority chain in product_normalizer.py:
      1. source_id (used as-is)
      2. _title_slug(title) — slug:…-sha1_12

    Safe to run multiple times — only touches NULL rows.
    """
    from app.normalization.models import NormalizedProduct
    from app.modules.ecommerce.normalizers.product_normalizer import _title_slug

    db = SessionLocal()
    updated_source = 0
    updated_slug = 0
    try:
        while True:
            batch = (
                db.query(NormalizedProduct)
                .filter(NormalizedProduct.canonical_product_id.is_(None))
                .limit(batch_size)
                .all()
            )
            if not batch:
                break
            for product in batch:
                if product.source_id:
                    product.canonical_product_id = product.source_id
                    updated_source += 1
                else:
                    slug = _title_slug(product.title)
                    if slug:
                        product.canonical_product_id = slug
                        updated_slug += 1
            db.commit()

        totals = {"updated_via_source_id": updated_source, "updated_via_slug": updated_slug}
        logger.info("Backfill canonical_product_id concluído", extra=totals)
        return totals
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def analytics_job(module: str | None = None, limit: int = 100) -> None:
    register_pipeline_modules()
    db = SessionLocal()
    try:
        modules = [module] if module else analytics_registry.modules()
        for module_name in modules:
            processor_type = analytics_registry.get(module_name)
            if not processor_type:
                continue
            logger.info("Starting analytics job", extra={"pipeline_module": module_name})
            processor_type(db).run(limit=limit)
    finally:
        db.close()

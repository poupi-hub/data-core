import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.analytics.registry import analytics_registry
from app.modules.registry import register_pipeline_modules
from app.normalization.registry import normalizer_registry
from database.models import CollectionRun, CollectionTarget, RunStatus
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
    db = SessionLocal()
    try:
        modules = [module] if module else normalizer_registry.modules()
        for module_name in modules:
            normalizer_types = normalizer_registry.all().get(module_name, [])
            for normalizer_type in normalizer_types:
                logger.info(
                    "Starting normalization job",
                    extra={
                        "pipeline_module": module_name,
                        "normalizer": normalizer_type.__name__,
                    },
                )
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
        query = db.query(CollectionTarget).filter(CollectionTarget.active.is_(True))
        if module:
            query = query.filter(CollectionTarget.module == module)
        if source:
            query = query.filter(CollectionTarget.source_name == source)
        if collector_name:
            query = query.filter(CollectionTarget.collector_name == collector_name)
        target_limit = max_targets if max_targets is not None else limit
        targets = query.order_by(CollectionTarget.created_at).limit(target_limit).all()
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
                raw_saved += _run_poupi_legacy_targets(
                    db,
                    available_targets,
                    delay_seconds=delay_seconds,
                    timeout_seconds=timeout_seconds,
                )
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
) -> int:
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
    return int(result.get("raw_saved_count", 0))


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

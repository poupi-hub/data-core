import logging
import traceback
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.metrics import collection_raw_duplicates_total, collection_raw_saved_total
from collectors.registry import registry
from core.config import settings
from database.models import (
    CollectedRecord,
    CollectionRun,
    CollectorDefinition,
    CollectorError,
    RunStatus,
)
from utils.hashing import stable_payload_hash
from utils.retry import retry_async

logger = logging.getLogger(__name__)


async def run_collector_by_name(collector_name: str, db: Session) -> CollectionRun:
    collector_type = registry.get(collector_name)
    metadata = collector_type.metadata

    definition = _ensure_collector_definition(db, collector_type)
    collector = collector_type(config=definition.config)
    run = CollectionRun(
        collector_id=definition.id,
        collector_name=metadata.name,
        collector_version=metadata.collector_version,
        raw_schema_name=metadata.raw_schema_name,
        raw_schema_version=metadata.raw_schema_version,
        module=collector.raw_module,
        domain=metadata.domain,
        source=metadata.source,
        source_name=metadata.source,
        status=RunStatus.running,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        items = await retry_async(
            collector.collect,
            max_attempts=settings.collector_default_max_retries,
            delay_seconds=settings.collector_default_retry_delay_seconds,
        )

        saved = 0
        raw_saved = 0
        raw_duplicates = 0
        domain_label = metadata.domain.value
        for item in items:
            item_saved = collector.save_raw(db, [item])
            raw_saved += item_saved
            if item_saved:
                # Increment Prometheus counter — wired here for Phase E (E-01 fix).
                # Metric defined in api/metrics.py; previously never incremented.
                collection_raw_saved_total.labels(
                    domain=domain_label,
                    collector_name=metadata.name,
                ).inc(item_saved)
            else:
                raw_duplicates += 1
                collection_raw_duplicates_total.labels(
                    domain=domain_label,
                    collector_name=metadata.name,
                ).inc()
            payload_hash = stable_payload_hash(item.payload)
            record = CollectedRecord(
                run_id=run.id,
                collector_name=metadata.name,
                domain=metadata.domain,
                source=metadata.source,
                external_id=item.external_id,
                source_url=item.source_url,
                payload=item.payload,
                payload_hash=payload_hash,
            )
            try:
                with db.begin_nested():
                    db.add(record)
                saved += 1
            except IntegrityError:
                logger.info("Duplicate collected record ignored", extra={"collector": metadata.name})

        run.status = RunStatus.success
        run.items_collected = saved
        run.raw_saved_count = raw_saved
        run.metadata_json = {
            **(run.metadata_json or {}),
            "duplicate_raw_count": raw_duplicates,
            "collector_version": metadata.collector_version,
            "raw_schema_name": metadata.raw_schema_name,
            "raw_schema_version": metadata.raw_schema_version,
        }
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
        return run

    except Exception as exc:
        db.rollback()
        run = db.merge(run)
        run.status = RunStatus.failed
        run.error_message = str(exc)
        run.error_count = 1
        run.finished_at = datetime.now(timezone.utc)
        db.add(
            CollectorError(
                run_id=run.id,
                collector_name=metadata.name,
                error_type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
                context={"collector": metadata.name},
            )
        )
        db.commit()
        db.refresh(run)
        logger.exception("Collector failed", extra={"collector": metadata.name})
        return run


def _ensure_collector_definition(db: Session, collector_type: type) -> CollectorDefinition:
    metadata = collector_type.metadata
    definition = (
        db.query(CollectorDefinition)
        .filter(CollectorDefinition.name == metadata.name)
        .one_or_none()
    )
    if definition:
        return definition

    definition = CollectorDefinition(
        name=metadata.name,
        domain=metadata.domain,
        source=metadata.source,
        enabled=True,
        config={},
    )
    db.add(definition)
    db.commit()
    db.refresh(definition)
    return definition

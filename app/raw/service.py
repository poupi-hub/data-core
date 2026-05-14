import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import CollectionRun, RunStatus
from app.raw.models import CollectorVersion, RawCollection
from app.raw.repository import RawRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawCollectionInput:
    module: str
    source_name: str
    collector_name: str
    collector_version: str = "1.0.0"
    raw_schema_name: str = "genericJson"
    raw_schema_version: str = "1.0.0"
    source_type: str | None = None
    source_id: str | None = None
    target_url: str | None = None
    endpoint: str | None = None
    method: str | None = "GET"
    request_params_json: dict[str, Any] | None = None
    request_headers_json: dict[str, Any] | None = None
    response_status: int | None = None
    response_headers_json: dict[str, Any] | None = None
    content_type: str | None = None
    raw_content: str | None = None
    raw_json: dict[str, Any] | list[Any] | None = None
    collected_at: datetime | None = None
    error_message: str | None = None
    metadata_json: dict[str, Any] = field(default_factory=dict)
    collection_metadata_json: dict[str, Any] = field(default_factory=dict)


class RawCollectionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = RawRepository(db)
        self._version_cache: set[tuple[str, ...]] = set()

    def save_html(
        self,
        *,
        module: str,
        source_name: str,
        collector_name: str,
        collector_version: str = "1.0.0",
        raw_schema_name: str = "htmlPage",
        raw_schema_version: str = "1.0.0",
        raw_content: str,
        source_type: str | None = "html",
        source_id: str | None = None,
        target_url: str | None = None,
        endpoint: str | None = None,
        method: str | None = "GET",
        response_status: int | None = None,
        response_headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        collection_metadata: dict[str, Any] | None = None,
    ) -> RawCollection:
        return self.save(
            RawCollectionInput(
                module=module,
                source_name=source_name,
                source_type=source_type,
                source_id=source_id,
                collector_name=collector_name,
                collector_version=collector_version,
                raw_schema_name=raw_schema_name,
                raw_schema_version=raw_schema_version,
                target_url=target_url,
                endpoint=endpoint,
                method=method,
                response_status=response_status,
                response_headers_json=response_headers,
                content_type="text/html",
                raw_content=raw_content,
                metadata_json=metadata or {},
                collection_metadata_json=collection_metadata or metadata or {},
            )
        )

    def save_json(
        self,
        *,
        module: str,
        source_name: str,
        collector_name: str,
        collector_version: str = "1.0.0",
        raw_schema_name: str = "genericJson",
        raw_schema_version: str = "1.0.0",
        raw_json: dict[str, Any] | list[Any],
        source_type: str | None = "api",
        source_id: str | None = None,
        target_url: str | None = None,
        endpoint: str | None = None,
        method: str | None = "GET",
        request_params: dict[str, Any] | None = None,
        request_headers: dict[str, Any] | None = None,
        response_status: int | None = None,
        response_headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        collection_metadata: dict[str, Any] | None = None,
    ) -> RawCollection:
        return self.save(
            RawCollectionInput(
                module=module,
                source_name=source_name,
                source_type=source_type,
                source_id=source_id,
                collector_name=collector_name,
                collector_version=collector_version,
                raw_schema_name=raw_schema_name,
                raw_schema_version=raw_schema_version,
                target_url=target_url,
                endpoint=endpoint,
                method=method,
                request_params_json=request_params,
                request_headers_json=request_headers,
                response_status=response_status,
                response_headers_json=response_headers,
                content_type="application/json",
                raw_json=raw_json,
                metadata_json=metadata or {},
                collection_metadata_json=collection_metadata or metadata or {},
            )
        )

    def save_text(
        self,
        *,
        module: str,
        source_name: str,
        collector_name: str,
        collector_version: str = "1.0.0",
        raw_schema_name: str = "textPayload",
        raw_schema_version: str = "1.0.0",
        raw_content: str,
        source_type: str | None = "text",
        source_id: str | None = None,
        target_url: str | None = None,
        endpoint: str | None = None,
        method: str | None = "GET",
        content_type: str | None = "text/plain",
        metadata: dict[str, Any] | None = None,
        collection_metadata: dict[str, Any] | None = None,
    ) -> RawCollection:
        return self.save(
            RawCollectionInput(
                module=module,
                source_name=source_name,
                source_type=source_type,
                source_id=source_id,
                collector_name=collector_name,
                collector_version=collector_version,
                raw_schema_name=raw_schema_name,
                raw_schema_version=raw_schema_version,
                target_url=target_url,
                endpoint=endpoint,
                method=method,
                content_type=content_type,
                raw_content=raw_content,
                metadata_json=metadata or {},
                collection_metadata_json=collection_metadata or metadata or {},
            )
        )

    def save_error(
        self,
        *,
        module: str,
        source_name: str,
        collector_name: str,
        collector_version: str = "1.0.0",
        raw_schema_name: str = "collectionError",
        raw_schema_version: str = "1.0.0",
        error_message: str,
        source_type: str | None = None,
        source_id: str | None = None,
        target_url: str | None = None,
        endpoint: str | None = None,
        method: str | None = "GET",
        response_status: int | None = None,
        metadata: dict[str, Any] | None = None,
        collection_metadata: dict[str, Any] | None = None,
    ) -> RawCollection:
        return self.save(
            RawCollectionInput(
                module=module,
                source_name=source_name,
                source_type=source_type,
                source_id=source_id,
                collector_name=collector_name,
                collector_version=collector_version,
                raw_schema_name=raw_schema_name,
                raw_schema_version=raw_schema_version,
                target_url=target_url,
                endpoint=endpoint,
                method=method,
                response_status=response_status,
                error_message=error_message,
                metadata_json=metadata or {},
                collection_metadata_json=collection_metadata or metadata or {},
            )
        )

    def save(self, item: RawCollectionInput) -> RawCollection:
        checksum = self.calculate_checksum(
            raw_content=item.raw_content,
            raw_json=item.raw_json,
            metadata={
                "module": item.module,
                "source_id": item.source_id,
                "source_name": item.source_name,
                "collector_name": item.collector_name,
                "collector_version": item.collector_version,
                "raw_schema_name": item.raw_schema_name,
                "raw_schema_version": item.raw_schema_version,
                "target_url": item.target_url,
                "endpoint": item.endpoint,
            },
        )
        existing = (
            self.db.query(RawCollection)
            .filter(
                RawCollection.module == item.module,
                RawCollection.source_name == item.source_name,
                RawCollection.checksum == checksum,
            )
            .one_or_none()
        )
        if existing:
            setattr(existing, "_raw_was_created", False)
            logger.info(
                "Duplicate RAW collection ignored",
                extra={
                    "raw_module": item.module,
                    "source_name": item.source_name,
                    "collector_name": item.collector_name,
                    "collector_version": item.collector_version,
                    "raw_schema_name": item.raw_schema_name,
                    "raw_schema_version": item.raw_schema_version,
                },
            )
            return existing

        raw = RawCollection(
            module=item.module,
            source_name=item.source_name,
            source_type=item.source_type,
            source_id=item.source_id,
            collector_name=item.collector_name,
            collector_version=item.collector_version,
            raw_schema_name=item.raw_schema_name,
            raw_schema_version=item.raw_schema_version,
            target_url=item.target_url,
            url=item.target_url,
            endpoint=item.endpoint,
            method=item.method,
            request_params_json=item.request_params_json,
            request_headers_json=item.request_headers_json,
            response_status=item.response_status,
            response_headers_json=item.response_headers_json,
            content_type=item.content_type,
            raw_content=item.raw_content,
            raw_json=item.raw_json,
            checksum=checksum,
            processing_status="normalization_pending" if item.error_message is None else "ignored",
            collected_at=item.collected_at or datetime.now(timezone.utc),
            error_message=item.error_message,
            metadata_json=item.metadata_json,
            collection_metadata_json=item.collection_metadata_json or item.metadata_json,
        )
        self.ensure_collector_version(
            module=item.module,
            source_name=item.source_name,
            collector_name=item.collector_name,
            collector_version=item.collector_version,
            raw_schema_name=item.raw_schema_name,
            raw_schema_version=item.raw_schema_version,
        )
        try:
            with self.db.begin_nested():
                saved = self.repository.add(raw)
        except IntegrityError:
            # Concurrent insert lost the race — re-fetch the winner row.
            existing = (
                self.db.query(RawCollection)
                .filter(
                    RawCollection.module == item.module,
                    RawCollection.source_name == item.source_name,
                    RawCollection.checksum == checksum,
                )
                .one()
            )
            setattr(existing, "_raw_was_created", False)
            logger.info(
                "RAW collection deduplicated (concurrent write)",
                extra={"raw_module": item.module, "source_name": item.source_name},
            )
            return existing

        setattr(saved, "_raw_was_created", True)
        logger.info(
            "RAW collection saved",
            extra={
                "raw_module": item.module,
                "source_name": item.source_name,
                "collector_name": item.collector_name,
                "collector_version": item.collector_version,
                "raw_schema_name": item.raw_schema_name,
                "raw_schema_version": item.raw_schema_version,
                "processing_status": saved.processing_status,
            },
        )
        return saved

    def ensure_collector_version(
        self,
        *,
        module: str,
        source_name: str,
        collector_name: str,
        collector_version: str,
        raw_schema_name: str,
        raw_schema_version: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CollectorVersion:
        cache_key = (module, source_name, collector_name, collector_version, raw_schema_name, raw_schema_version)
        if cache_key in self._version_cache:
            return None  # type: ignore[return-value]  # caller never uses the return value
        existing = (
            self.db.query(CollectorVersion)
            .filter(
                CollectorVersion.module == module,
                CollectorVersion.source_name == source_name,
                CollectorVersion.collector_name == collector_name,
                CollectorVersion.collector_version == collector_version,
                CollectorVersion.raw_schema_name == raw_schema_name,
                CollectorVersion.raw_schema_version == raw_schema_version,
            )
            .one_or_none()
        )
        if existing:
            self._version_cache.add(cache_key)
            return existing
        version = CollectorVersion(
            module=module,
            source_name=source_name,
            collector_name=collector_name,
            collector_version=collector_version,
            raw_schema_name=raw_schema_name,
            raw_schema_version=raw_schema_version,
            description=description,
            metadata_json=metadata or {},
        )
        self.db.add(version)
        self.db.flush()
        self._version_cache.add(cache_key)
        return version

    def start_run(
        self,
        *,
        module: str,
        source_name: str,
        collector_name: str,
        collector_version: str | None = None,
        raw_schema_name: str | None = None,
        raw_schema_version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CollectionRun:
        run = CollectionRun(
            module=module,
            source_name=source_name,
            collector_name=collector_name,
            collector_version=collector_version,
            raw_schema_name=raw_schema_name,
            raw_schema_version=raw_schema_version,
            source=source_name,
            status=RunStatus.running,
            started_at=datetime.now(timezone.utc),
            metadata_json={
                **(metadata or {}),
                "collector_version": collector_version,
                "raw_schema_name": raw_schema_name,
                "raw_schema_version": raw_schema_version,
            },
        )
        self.db.add(run)
        self.db.flush()
        return run

    def finish_run(
        self,
        run: CollectionRun,
        *,
        status: RunStatus = RunStatus.success,
        raw_saved_count: int = 0,
        error_count: int = 0,
        error_message: str | None = None,
    ) -> CollectionRun:
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        run.raw_saved_count = raw_saved_count
        run.items_collected = raw_saved_count
        run.error_count = error_count
        run.error_message = error_message
        self.db.flush()
        return run

    @staticmethod
    def calculate_checksum(
        *,
        raw_content: str | None,
        raw_json: dict[str, Any] | list[Any] | None,
        metadata: dict[str, Any],
    ) -> str:
        payload = {
            "raw_content": raw_content,
            "raw_json": raw_json,
            "metadata": metadata,
        }
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


RawService = RawCollectionService

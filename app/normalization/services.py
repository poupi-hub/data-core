import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.documentation.lineage import LineageService
from app.normalization.models import (
    NormalizedCryptoSnapshot,
    NormalizedMarketCandle,
    NormalizedProduct,
    NormalizedRealEstateListing,
    NormalizedSportsOdd,
    NormalizerVersion,
)
from app.raw.models import RawCollection
from app.raw.repository import RawRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizationResult:
    module: str
    loaded_raw: int
    normalized: int
    failed: int
    elapsed_seconds: float


_ALL_NORMALIZED_MODELS = None  # populated lazily to avoid import-time circular deps


def _all_normalized_models() -> tuple[type, ...]:
    global _ALL_NORMALIZED_MODELS
    if _ALL_NORMALIZED_MODELS is None:
        _ALL_NORMALIZED_MODELS = (
            NormalizedProduct,
            NormalizedRealEstateListing,
            NormalizedCryptoSnapshot,
            NormalizedMarketCandle,
            NormalizedSportsOdd,
        )
    return _ALL_NORMALIZED_MODELS


class BaseNormalizer(ABC):
    module: str
    normalizer_name: str | None = None
    normalizer_version: str = "1.0.0"
    supported_raw_schema_name: str | None = None
    supported_raw_schema_version: str | None = None
    supported_source_name: str | None = None
    # Override in subclasses to restrict stamp/lineage queries to only the relevant model(s).
    normalized_model_classes: tuple[type, ...] = ()

    def __init__(self, db: Session) -> None:
        self.db = db
        self.raw_repository = RawRepository(db)
        self._version_ensured: bool = False

    @property
    def _stamp_models(self) -> tuple[type, ...]:
        return self.normalized_model_classes or _all_normalized_models()

    @classmethod
    def supports_raw(
        cls,
        *,
        source_name: str | None = None,
        raw_schema_name: str | None = None,
        raw_schema_version: str | None = None,
    ) -> bool:
        if cls.supported_source_name and cls.supported_source_name != source_name:
            return False
        if cls.supported_raw_schema_name and cls.supported_raw_schema_name != raw_schema_name:
            return False
        if cls.supported_raw_schema_version and cls.supported_raw_schema_version != raw_schema_version:
            return False
        return True

    def load_raw(self, *, limit: int = 100) -> list[RawCollection]:
        raws = self.raw_repository.pending_for_module(
            self.module,
            limit=limit,
            source_name=self.supported_source_name,
            raw_schema_name=self.supported_raw_schema_name,
            raw_schema_version=self.supported_raw_schema_version,
        )
        # Keep Python-level guard in case a subclass overrides supports_raw() with extra logic.
        return [
            raw
            for raw in raws
            if self.supports_raw(
                source_name=raw.source_name,
                raw_schema_name=raw.raw_schema_name,
                raw_schema_version=raw.raw_schema_version,
            )
        ]

    @abstractmethod
    def normalize(self, raw: RawCollection) -> object | list[object] | None:
        """Transform one RAW item into normalized domain objects."""

    @abstractmethod
    def save_normalized(self, raw: RawCollection, normalized: object | list[object] | None) -> int:
        """Persist normalized objects and return count."""

    def run(self, *, limit: int = 100) -> NormalizationResult:
        started = time.perf_counter()
        raws = self.load_raw(limit=limit)
        saved = 0
        failed = 0

        for raw in raws:
            try:
                normalized = self.normalize(raw)
                raw_saved = self.save_normalized(raw, normalized)
                saved += raw_saved
                if raw_saved:
                    self.stamp_normalized(raw)
                    self.ensure_normalizer_version(raw)
                raw.processing_status = "normalized" if raw_saved else "ignored"
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                raw = self.db.merge(raw)
                raw.processing_status = "normalization_failed"
                raw.error_message = str(exc)
                self.db.commit()
                failed += 1
                logger.exception("Normalization failed", extra={"pipeline_module": self.module, "raw_id": str(raw.id)})

        elapsed = time.perf_counter() - started
        logger.info(
            "Normalization finished",
            extra={
                "pipeline_module": self.module,
                "loaded_raw": len(raws),
                "normalized": saved,
                "failed": failed,
                "elapsed_seconds": round(elapsed, 3),
            },
        )
        return NormalizationResult(
            module=self.module,
            loaded_raw=len(raws),
            normalized=saved,
            failed=failed,
            elapsed_seconds=elapsed,
        )

    @property
    def effective_normalizer_name(self) -> str:
        return self.normalizer_name or self.__class__.__name__

    def normalization_metadata(self, raw: RawCollection) -> dict[str, Any]:
        return {
            "module": raw.module,
            "source_name": raw.source_name,
            "raw_collection_id": str(raw.id),
        }

    def stamp_normalized(self, raw: RawCollection) -> None:
        now = datetime.now(timezone.utc)
        for model in self._stamp_models:
            self.db.query(model).filter(
                model.raw_collection_id == raw.id,
                model.normalizer_name.is_(None),
            ).update(
                {
                    "normalizer_name": self.effective_normalizer_name,
                    "normalizer_version": self.normalizer_version,
                    "normalized_at": now,
                    "normalization_metadata_json": self.normalization_metadata(raw),
                    "source_raw_schema_name": raw.raw_schema_name,
                    "source_raw_schema_version": raw.raw_schema_version,
                    "source_collector_name": raw.collector_name,
                    "source_collector_version": raw.collector_version,
                },
                synchronize_session=False,
            )
        self.record_lineage_for_normalized(raw)

    def record_lineage_for_normalized(self, raw: RawCollection) -> None:
        lineage = LineageService(self.db)
        for model in self._stamp_models:
            rows = (
                self.db.query(model)
                .filter(
                    model.raw_collection_id == raw.id,
                    model.normalizer_name == self.effective_normalizer_name,
                    model.normalizer_version == self.normalizer_version,
                )
                .all()
            )
            for row in rows:
                lineage.record_normalized(
                    raw=raw,
                    normalizer_name=self.effective_normalizer_name,
                    normalizer_version=self.normalizer_version,
                    normalized_record_type=model.__tablename__,
                    normalized_record_id=row.id,
                    metadata={"normalized_entity": model.__tablename__},
                )

    def ensure_normalizer_version(self, raw: RawCollection) -> NormalizerVersion | None:
        if self._version_ensured:
            return None
        existing = (
            self.db.query(NormalizerVersion)
            .filter(
                NormalizerVersion.module == self.module,
                NormalizerVersion.source_name == raw.source_name,
                NormalizerVersion.raw_schema_name == raw.raw_schema_name,
                NormalizerVersion.raw_schema_version == raw.raw_schema_version,
                NormalizerVersion.normalizer_name == self.effective_normalizer_name,
                NormalizerVersion.normalizer_version == self.normalizer_version,
            )
            .one_or_none()
        )
        if existing:
            self._version_ensured = True
            return existing
        version = NormalizerVersion(
            module=self.module,
            source_name=raw.source_name,
            raw_schema_name=raw.raw_schema_name,
            raw_schema_version=raw.raw_schema_version,
            normalizer_name=self.effective_normalizer_name,
            normalizer_version=self.normalizer_version,
        )
        self.db.add(version)
        self.db.flush()
        self._version_ensured = True
        return version

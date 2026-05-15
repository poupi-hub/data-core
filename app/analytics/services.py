import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.analytics.models import (
    CryptoAnalytics,
    ProductPriceAnalytics,
    RealEstateAnalytics,
    SportsOddsAnalytics,
    TradingAnalytics,
)
from app.documentation.lineage import LineageService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalyticsResult:
    module: str
    loaded_normalized: int
    processed: int
    failed: int
    elapsed_seconds: float


class BaseAnalyticsProcessor(ABC):
    module: str
    analytics_processor_name: str | None = None
    analytics_processor_version: str = "1.0.0"

    def __init__(self, db: Session) -> None:
        self.db = db

    @abstractmethod
    def load_normalized(self, *, limit: int = 100) -> list[object]:
        """Load normalized records with pending analytics status."""

    @abstractmethod
    def calculate(self, normalized: object) -> object | None:
        """Calculate analytics for one normalized record."""

    @abstractmethod
    def save_analytics(self, normalized: object, analytics: object | None) -> int:
        """Persist analytics and return count."""

    def run(self, *, limit: int = 100) -> AnalyticsResult:
        started = time.perf_counter()
        records = self.load_normalized(limit=limit)
        processed = 0
        failed = 0

        for record in records:
            try:
                analytics = self.calculate(record)
                record_saved = self.save_analytics(record, analytics)
                processed += record_saved
                if record_saved:
                    self.stamp_analytics(record)
                if hasattr(record, "analytics_status"):
                    record.analytics_status = "processed"
                self.db.commit()
            except Exception:
                self.db.rollback()
                record = self.db.merge(record)
                if hasattr(record, "analytics_status"):
                    record.analytics_status = "failed"
                self.db.commit()
                failed += 1
                logger.exception("Analytics processing failed", extra={"pipeline_module": self.module})

        elapsed = time.perf_counter() - started
        logger.info(
            "Analytics processing finished",
            extra={
                "pipeline_module": self.module,
                "loaded_normalized": len(records),
                "processed": processed,
                "failed": failed,
                "elapsed_seconds": round(elapsed, 3),
            },
        )
        return AnalyticsResult(
            module=self.module,
            loaded_normalized=len(records),
            processed=processed,
            failed=failed,
            elapsed_seconds=elapsed,
        )

    def stamp_analytics(self, normalized: object) -> None:
        normalizer_name = getattr(normalized, "normalizer_name", None)
        normalizer_version = getattr(normalized, "normalizer_version", None)
        processor_name = self.analytics_processor_name or self.__class__.__name__
        processor_version = self.analytics_processor_version
        lineage = LineageService(self.db)
        if self.module == "ecommerce":
            rows = self.db.query(ProductPriceAnalytics).filter(
                ProductPriceAnalytics.product_id == normalized.id,
            ).all()
            for row in rows:
                row.source_normalizer_name = normalizer_name
                row.source_normalizer_version = normalizer_version
                lineage.attach_analytics(
                    normalized_record_type="normalized_products",
                    normalized_record_id=normalized.id,
                    analytics_processor_name=processor_name,
                    analytics_processor_version=processor_version,
                    analytics_record_type="product_price_analytics",
                    analytics_record_id=row.id,
                )
        elif self.module == "real_estate":
            rows = self.db.query(RealEstateAnalytics).filter(
                RealEstateAnalytics.listing_id == normalized.id,
            ).all()
            for row in rows:
                row.source_normalizer_name = normalizer_name
                row.source_normalizer_version = normalizer_version
                lineage.attach_analytics(
                    normalized_record_type="normalized_real_estate_listings",
                    normalized_record_id=normalized.id,
                    analytics_processor_name=processor_name,
                    analytics_processor_version=processor_version,
                    analytics_record_type="real_estate_analytics",
                    analytics_record_id=row.id,
                )
        elif self.module == "crypto":
            rows = self.db.query(CryptoAnalytics).filter(
                CryptoAnalytics.symbol == normalized.symbol,
            ).all()
            for row in rows:
                row.source_normalizer_name = normalizer_name
                row.source_normalizer_version = normalizer_version
                lineage.attach_analytics(
                    normalized_record_type="normalized_crypto_snapshots",
                    normalized_record_id=normalized.id,
                    analytics_processor_name=processor_name,
                    analytics_processor_version=processor_version,
                    analytics_record_type="crypto_analytics",
                    analytics_record_id=row.id,
                )
        elif self.module == "trading":
            rows = self.db.query(TradingAnalytics).filter(TradingAnalytics.market_candle_id == normalized.id).all()
            if not rows:
                rows = self.db.query(TradingAnalytics).filter(
                    TradingAnalytics.symbol == normalized.symbol,
                    TradingAnalytics.timeframe == normalized.timeframe,
                    TradingAnalytics.market_candle_id.is_(None),
                ).all()
            for row in rows:
                row.source_normalizer_name = normalizer_name
                row.source_normalizer_version = normalizer_version
                lineage.attach_analytics(
                    normalized_record_type="normalized_market_candles",
                    normalized_record_id=normalized.id,
                    analytics_processor_name=processor_name,
                    analytics_processor_version=processor_version,
                    analytics_record_type="trading_analytics",
                    analytics_record_id=row.id,
                )
        elif self.module == "sports_odds":
            rows = self.db.query(SportsOddsAnalytics).filter(
                SportsOddsAnalytics.event_id == normalized.event_external_id,
                SportsOddsAnalytics.market_type == normalized.market_type,
                SportsOddsAnalytics.selection == normalized.selection,
            ).all()
            for row in rows:
                row.source_normalizer_name = normalizer_name
                row.source_normalizer_version = normalizer_version
                lineage.attach_analytics(
                    normalized_record_type="normalized_sports_odds",
                    normalized_record_id=normalized.id,
                    analytics_processor_name=processor_name,
                    analytics_processor_version=processor_version,
                    analytics_record_type="sports_odds_analytics",
                    analytics_record_id=row.id,
                )

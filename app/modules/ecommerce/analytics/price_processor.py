import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.analytics.models import ProductPriceAnalytics
from app.analytics.services import BaseAnalyticsProcessor
from app.normalization.models import NormalizedProduct


class ProductPriceAnalyticsProcessor(BaseAnalyticsProcessor):
    module = "ecommerce"

    def load_normalized(self, *, limit: int = 100) -> list[NormalizedProduct]:
        return (
            self.db.query(NormalizedProduct)
            .filter(NormalizedProduct.analytics_status == "pending")
            .order_by(NormalizedProduct.collected_at)
            .limit(limit)
            .all()
        )

    def _base_query(self, normalized: NormalizedProduct, days: int):
        """Return a base query filtered to the same canonical product within `days`."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        q = self.db.query(NormalizedProduct).filter(
            NormalizedProduct.price.isnot(None),
            NormalizedProduct.collected_at >= cutoff,
        )
        # Prefer canonical_product_id (cross-source), fall back to source_id, then store
        if normalized.canonical_product_id and not normalized.canonical_product_id.startswith("slug:"):
            q = q.filter(NormalizedProduct.canonical_product_id == normalized.canonical_product_id)
        elif normalized.source_id:
            q = q.filter(NormalizedProduct.source_id == normalized.source_id)
        elif normalized.store_name:
            q = q.filter(NormalizedProduct.store_name == normalized.store_name)
        else:
            return None
        return q

    def _price_stats(self, normalized: NormalizedProduct, days: int) -> tuple[float | None, float | None]:
        """Returns (avg, stddev) of price for the same canonical product in the last `days` days."""
        base = self._base_query(normalized, days)
        if base is None:
            return None, None
        q = base.with_entities(
            func.avg(NormalizedProduct.price).label("avg"),
            func.stddev_pop(NormalizedProduct.price).label("stddev"),
        )
        row = q.one()
        avg = float(row.avg) if row.avg is not None else None
        stddev = float(row.stddev) if row.stddev is not None else None
        return avg, stddev

    def _price_minmax(self, normalized: NormalizedProduct, days: int) -> tuple[float | None, float | None]:
        """Returns (min, max) of price for the same canonical product in the last `days` days."""
        base = self._base_query(normalized, days)
        if base is None:
            return None, None
        q = base.with_entities(
            func.min(NormalizedProduct.price).label("min_p"),
            func.max(NormalizedProduct.price).label("max_p"),
        )
        row = q.one()
        min_p = float(row.min_p) if row.min_p is not None else None
        max_p = float(row.max_p) if row.max_p is not None else None
        return min_p, max_p

    def calculate(self, normalized: NormalizedProduct) -> dict:
        price = float(normalized.price) if normalized.price is not None else None

        avg_7d, _ = self._price_stats(normalized, 7)
        avg_30d, stddev_30d = self._price_stats(normalized, 30)
        min_90d, max_90d = self._price_minmax(normalized, 90)

        price_score: float | None = None
        if price is not None and avg_30d is not None and stddev_30d is not None and stddev_30d > 0:
            z = (price - avg_30d) / stddev_30d
            # Clamp to [-3, 3] and invert: lower price → higher score (0–1)
            z_clamped = max(-3.0, min(3.0, z))
            price_score = round((1.0 - (z_clamped + 3.0) / 6.0), 4)
        elif price is not None and min_90d is not None and max_90d is not None and max_90d > min_90d:
            # Fallback: position within 90d range, inverted
            price_score = round(1.0 - (price - min_90d) / (max_90d - min_90d), 4)
        elif price is not None:
            # First snapshot or flat history: neutral score until there is enough variance.
            price_score = 0.5

        return {
            "avg_price_7d": round(avg_7d, 2) if avg_7d is not None else price,
            "avg_price_30d": round(avg_30d, 2) if avg_30d is not None else price,
            "min_price_90d": round(min_90d, 2) if min_90d is not None else price,
            "max_price_90d": round(max_90d, 2) if max_90d is not None else price,
            "price_score": price_score,
        }

    def save_analytics(self, normalized: NormalizedProduct, analytics: object | None) -> int:
        if not isinstance(analytics, dict):
            return 0
        self.db.add(
            ProductPriceAnalytics(
                product_id=normalized.id,
                source_normalizer_name=normalized.normalizer_name,
                source_normalizer_version=normalized.normalizer_version,
                **analytics,
            )
        )
        self.db.flush()
        return 1

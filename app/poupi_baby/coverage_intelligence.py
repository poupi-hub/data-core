from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from api import metrics
from app.analytics.models import ProductPriceAnalytics
from app.normalization.models import NormalizedProduct
from app.raw.models import RawCollection
from database.models import CollectionTarget

FRESH_HOURS = 6
GROWTH_HOURS = 24
MIN_MARKETPLACES_PER_PRODUCT = 2


@dataclass(frozen=True)
class BabyCoverageSnapshot:
    products: int
    marketplaces: int
    raw_24h: int
    normalized_24h: int
    price_history_24h: int
    catalog_coverage_rate: float
    products_below_target: int


def compute_baby_coverage_intelligence(db: Session) -> BabyCoverageSnapshot:
    now = datetime.now(timezone.utc)
    fresh_since = now - timedelta(hours=FRESH_HOURS)
    growth_since = now - timedelta(hours=GROWTH_HOURS)

    targets = (
        db.query(CollectionTarget)
        .filter(CollectionTarget.module == "ecommerce", CollectionTarget.active.is_(True))
        .all()
    )
    target_products = sorted({
        (t.metadata_json or {}).get("product_seed") or t.target_url
        for t in targets
    })
    target_marketplaces = sorted({t.source_name for t in targets})
    target_by_url = {
        (t.source_name, t.target_url): (t.metadata_json or {}).get("product_seed") or t.target_url
        for t in targets
    }

    fresh_rows = (
        db.query(
            NormalizedProduct.canonical_product_id,
            NormalizedProduct.store_name,
            NormalizedProduct.source_url,
            func.max(NormalizedProduct.collected_at).label("last_seen"),
        )
        .filter(NormalizedProduct.collected_at >= fresh_since)
        .filter(NormalizedProduct.price.isnot(None))
        .filter(NormalizedProduct.store_name.isnot(None))
        .group_by(
            NormalizedProduct.canonical_product_id,
            NormalizedProduct.store_name,
            NormalizedProduct.source_url,
        )
        .all()
    )

    active_by_product: dict[str, set[str]] = {p: set() for p in target_products}
    active_by_marketplace: dict[str, set[str]] = {m: set() for m in target_marketplaces}
    for product_id, marketplace, source_url, _last_seen in fresh_rows:
        product = target_by_url.get((marketplace, source_url), product_id or "unknown")
        if product in active_by_product and marketplace:
            active_by_product[product].add(marketplace)
            active_by_marketplace.setdefault(marketplace, set()).add(product)

    below_target = 0
    products_with_target_coverage = 0
    for product in target_products:
        coverage = len(active_by_product.get(product, set()))
        metrics.baby_coverage_per_product.labels(product=product).set(coverage)
        if coverage >= MIN_MARKETPLACES_PER_PRODUCT:
            products_with_target_coverage += 1
        else:
            below_target += 1
        for marketplace in target_marketplaces:
            active = 1 if marketplace in active_by_product.get(product, set()) else 0
            metrics.baby_product_marketplace_active.labels(
                product=product, marketplace=marketplace
            ).set(active)

    total_products = max(len(target_products), 1)
    catalog_rate = products_with_target_coverage / total_products * 100
    metrics.baby_coverage_score.set(catalog_rate)
    metrics.baby_products_below_coverage_target.set(below_target)

    for marketplace in target_marketplaces:
        monitored = sum(1 for t in targets if t.source_name == marketplace)
        active = len(active_by_marketplace.get(marketplace, set()))
        coverage_rate = (active / monitored * 100) if monitored else 0
        metrics.baby_marketplace_coverage_rate.labels(marketplace=marketplace).set(coverage_rate)

        last_price = (
            db.query(func.max(NormalizedProduct.collected_at))
            .filter(NormalizedProduct.store_name == marketplace)
            .filter(NormalizedProduct.price.isnot(None))
            .scalar()
        )
        age_seconds = _age_seconds(now, last_price)
        freshness = max(0.0, 100.0 - (age_seconds / (FRESH_HOURS * 3600) * 100.0))
        metrics.baby_marketplace_last_price_age_seconds.labels(marketplace=marketplace).set(age_seconds)
        metrics.marketplace_freshness_score.labels(marketplace=marketplace).set(freshness)

        raw_24h = (
            db.query(func.count(RawCollection.id))
            .filter(RawCollection.module == "ecommerce")
            .filter(RawCollection.source_name == marketplace)
            .filter(RawCollection.collected_at >= growth_since)
            .scalar()
            or 0
        )
        normalized_24h = (
            db.query(func.count(NormalizedProduct.id))
            .filter(NormalizedProduct.store_name == marketplace)
            .filter(NormalizedProduct.collected_at >= growth_since)
            .scalar()
            or 0
        )
        metrics.baby_raw_collections_24h.labels(marketplace=marketplace).set(raw_24h)
        metrics.baby_normalized_products_24h.labels(marketplace=marketplace).set(normalized_24h)
        success_rate = (normalized_24h / raw_24h * 100) if raw_24h else 100
        metrics.normalized_success_rate.labels(marketplace=marketplace).set(success_rate)
        useful_rate = (active / monitored * 100) if monitored else 0
        metrics.useful_offer_rate.labels(marketplace=marketplace).set(useful_rate)

    raw_total = (
        db.query(func.count(RawCollection.id))
        .filter(RawCollection.module == "ecommerce")
        .filter(RawCollection.collected_at >= growth_since)
        .scalar()
        or 0
    )
    normalized_total = (
        db.query(func.count(NormalizedProduct.id))
        .filter(NormalizedProduct.collected_at >= growth_since)
        .scalar()
        or 0
    )
    price_history_total = (
        db.query(func.count(ProductPriceAnalytics.id))
        .filter(ProductPriceAnalytics.calculated_at >= growth_since)
        .scalar()
        or 0
    )
    metrics.price_history_growth_rate.set(price_history_total)

    return BabyCoverageSnapshot(
        products=len(target_products),
        marketplaces=len(target_marketplaces),
        raw_24h=int(raw_total),
        normalized_24h=int(normalized_total),
        price_history_24h=int(price_history_total),
        catalog_coverage_rate=round(catalog_rate, 2),
        products_below_target=below_target,
    )


def _age_seconds(now: datetime, ts: datetime | None) -> float:
    if ts is None:
        return 10**9
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds())

from app.analytics.registry import analytics_registry
from app.modules.crypto.analytics.processor import CryptoAnalyticsProcessor
from app.modules.crypto.normalizers.snapshot_normalizer import CryptoSnapshotNormalizer
from app.modules.ecommerce.analytics.price_processor import ProductPriceAnalyticsProcessor
from app.modules.ecommerce.normalizers.poupi_legacy_scraped_product_v1_normalizer import (
    PoupiLegacyScrapedProductV1Normalizer,
)
from app.modules.ecommerce.normalizers.product_normalizer import EcommerceProductNormalizer
from app.modules.sports_odds.analytics.processor import SportsOddsAnalyticsProcessor
from app.modules.sports_odds.normalizers.odds_normalizer import SportsOddsNormalizer
from app.modules.trading.analytics.processor import TradingAnalyticsProcessor
from app.modules.trading.normalizers.candle_normalizer import TradingCandleNormalizer
from app.normalization.registry import normalizer_registry


def register_pipeline_modules() -> None:
    normalizer_registry.register("ecommerce", PoupiLegacyScrapedProductV1Normalizer)
    normalizer_registry.register("ecommerce", EcommerceProductNormalizer)
    normalizer_registry.register("crypto", CryptoSnapshotNormalizer)
    normalizer_registry.register("trading", TradingCandleNormalizer)
    normalizer_registry.register("sports_odds", SportsOddsNormalizer)

    analytics_registry.register("ecommerce", ProductPriceAnalyticsProcessor)
    analytics_registry.register("crypto", CryptoAnalyticsProcessor)
    analytics_registry.register("trading", TradingAnalyticsProcessor)
    analytics_registry.register("sports_odds", SportsOddsAnalyticsProcessor)

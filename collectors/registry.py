from collectors.base import BaseCollector
from collectors.crypto.crypto_coin_ohlcv import CryptoCoinOHLCVCollector
from collectors.crypto.generic_price import GenericCryptoPriceCollector
from collectors.ecommerce.generic_product import GenericProductCollector
from collectors.sports_betting.generic_odds import GenericSportsOddsCollector

CollectorType = type[BaseCollector]


class CollectorRegistry:
    def __init__(self) -> None:
        self._collectors: dict[str, CollectorType] = {}

    def register(self, collector_type: CollectorType) -> None:
        self._collectors[collector_type.metadata.name] = collector_type

    def get(self, name: str) -> CollectorType:
        try:
            return self._collectors[name]
        except KeyError as exc:
            raise KeyError(f"Collector not registered: {name}") from exc

    def all(self) -> list[CollectorType]:
        return list(self._collectors.values())

    def names(self) -> list[str]:
        return sorted(self._collectors.keys())


registry = CollectorRegistry()

# ── Active verticals ──────────────────────────────────────────────────────────
registry.register(GenericProductCollector)
registry.register(GenericCryptoPriceCollector)
registry.register(CryptoCoinOHLCVCollector)
registry.register(GenericSportsOddsCollector)
# Jobs (SUNSET 2026-06-09): collectors/jobs/ removed — EXTERNAL_DEGRADED (Gupy frozen 2025-08)
# Real Estate (SUNSET 2026-06-09): collectors/real_estate/ removed

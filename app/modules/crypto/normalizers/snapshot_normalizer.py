from datetime import datetime, timezone
from typing import Any

from app.normalization.models import NormalizedCryptoSnapshot, NormalizedMarketCandle
from app.normalization.services import BaseNormalizer
from app.raw.models import RawCollection


class CryptoSnapshotNormalizer(BaseNormalizer):
    module = "crypto"
    normalizer_name = "crypto_snapshot_normalizer"
    normalizer_version = "1.0.0"
    normalized_model_classes = (NormalizedCryptoSnapshot, NormalizedMarketCandle)

    def normalize(self, raw: RawCollection) -> dict[str, Any] | None:
        if not isinstance(raw.raw_json, dict):
            return None
        payload = raw.raw_json
        if {"open", "high", "low", "close"}.issubset(payload.keys()):
            return {"kind": "candle", **payload}
        return {
            "kind": "snapshot",
            "source": raw.source_name,
            "symbol": payload.get("symbol") or payload.get("pair") or raw.source_id,
            "price": payload.get("price") or payload.get("last") or payload.get("last_price"),
            "volume": payload.get("volume") or payload.get("volume_24h"),
            "market_cap": payload.get("market_cap"),
            "change_24h": payload.get("change_24h") or payload.get("percentage"),
            "collected_at": raw.collected_at or datetime.now(timezone.utc),
        }

    def save_normalized(self, raw: RawCollection, normalized: object | list[object] | None) -> int:
        if not isinstance(normalized, dict):
            return 0
        if normalized.pop("kind", None) == "candle":
            self.db.add(
                NormalizedMarketCandle(
                    raw_collection_id=raw.id,
                    source=raw.source_name,
                    symbol=normalized.get("symbol") or raw.source_id or "unknown",
                    timeframe=normalized.get("timeframe") or raw.metadata_json.get("timeframe") or "unknown",
                    open=normalized.get("open"),
                    high=normalized.get("high"),
                    low=normalized.get("low"),
                    close=normalized.get("close"),
                    volume=normalized.get("volume"),
                    timestamp=_parse_datetime(normalized.get("timestamp")) or raw.collected_at,
                )
            )
        else:
            self.db.add(NormalizedCryptoSnapshot(raw_collection_id=raw.id, **normalized))
        self.db.flush()
        return 1


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

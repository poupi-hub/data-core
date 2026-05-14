from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from app.normalization.models import NormalizedProduct
from app.normalization.services import BaseNormalizer
from app.raw.models import RawCollection


class EcommerceProductNormalizer(BaseNormalizer):
    module = "ecommerce"
    normalizer_name = "generic_product_normalizer"
    normalizer_version = "1.0.0"

    def normalize(self, raw: RawCollection) -> dict[str, Any] | None:
        if not isinstance(raw.raw_json, dict):
            raw.error_message = "ignored_by_normalizer: raw_json is not an object"
            return None
        payload = raw.raw_json.get("scrapedProduct") or raw.raw_json.get("scraped_product") or raw.raw_json
        if not isinstance(payload, dict):
            raw.error_message = "ignored_by_normalizer: product payload is not an object"
            return None
        if payload.get("success") is False:
            raw.error_message = f"ignored_by_normalizer: payload success=false: {payload.get('error') or 'unknown_error'}"
            return None
        title = _clean_text(
            payload.get("title")
            or payload.get("name")
            or payload.get("productName")
            or payload.get("product_name")
        )
        price = _parse_decimal(
            payload.get("price")
            or payload.get("current_price")
            or payload.get("sale_price")
            or payload.get("price_text")
            or payload.get("priceText")
        )
        if not title and price is None:
            raw.error_message = "ignored_by_normalizer: missing title/name and parseable price"
            return None
        raw.error_message = None
        return {
            "source_id": raw.source_id,
            "external_id": payload.get("external_id") or raw.source_id or raw.target_url,
            "canonical_product_id": payload.get("canonical_product_id"),
            "title": title,
            "brand": _clean_text(payload.get("brand") or payload.get("manufacturer")),
            "price": price,
            "currency": str(payload.get("currency") or "BRL").upper(),
            "availability": _availability_text(payload),
            "store_name": payload.get("store_name") or payload.get("store") or raw.source_name,
            "city": payload.get("city") or raw.metadata_json.get("city"),
            "state": payload.get("state") or raw.metadata_json.get("state"),
            "shipping_price": _parse_decimal(payload.get("shipping_price") or payload.get("shipping")),
            "collected_at": raw.collected_at or datetime.now(timezone.utc),
        }

    def normalization_metadata(self, raw: RawCollection) -> dict[str, Any]:
        metadata = super().normalization_metadata(raw)
        payload = raw.raw_json.get("scrapedProduct") if isinstance(raw.raw_json, dict) else None
        if isinstance(payload, dict):
            metadata.update(
                {
                    "raw_success": payload.get("success"),
                    "raw_error": payload.get("error"),
                    "raw_store": payload.get("store"),
                    "target_url": raw.target_url,
                }
            )
        return metadata

    def save_normalized(self, raw: RawCollection, normalized: object | list[object] | None) -> int:
        if not isinstance(normalized, dict):
            return 0
        self.db.add(NormalizedProduct(raw_collection_id=raw.id, **normalized))
        self.db.flush()
        return 1


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _parse_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, dict):
        for key in ("amount", "value", "current", "sale", "price", "number"):
            parsed = _parse_decimal(value.get(key))
            if parsed is not None:
                return parsed
        cents = value.get("cents") or value.get("amount_cents")
        if cents not in (None, ""):
            parsed_cents = _parse_decimal(cents)
            return parsed_cents / Decimal("100") if parsed_cents is not None else None
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed = _parse_decimal(item)
            if parsed is not None:
                return parsed
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"[-+]?\d[\d.\s]*,\d{1,2}|[-+]?\d+(?:\.\d{1,2})?", text)
    if match:
        text = match.group(0)
    text = text.replace("R$", "").replace("\xa0", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _availability_text(payload: dict[str, Any]) -> str | None:
    availability = payload.get("availability")
    if availability is not None:
        return str(availability)
    for key in ("available", "in_stock", "isAvailable"):
        if key in payload:
            return "in_stock" if payload.get(key) else "out_of_stock"
    return None

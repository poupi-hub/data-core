from app.modules.real_estate.parsers.generic_parser import GenericRealEstateParser
from app.normalization.models import NormalizedRealEstateListing
from app.normalization.services import BaseNormalizer
from app.raw.models import RawCollection


class RealEstateListingNormalizer(BaseNormalizer):
    module = "real_estate"
    normalizer_name = "generic_real_estate_normalizer"
    normalizer_version = "1.0.0"
    normalized_model_classes = (NormalizedRealEstateListing,)

    def __init__(self, db):
        super().__init__(db)
        self.parser = GenericRealEstateParser()

    def normalize(self, raw: RawCollection) -> dict | None:
        if not raw.raw_content:
            return None
        parsed = self.parser.parse(raw.raw_content, raw.target_url or raw.url or raw.endpoint or "")
        return {
            "source_id": raw.source_id,
            "external_id": parsed.external_id,
            "url": parsed.url,
            "title": parsed.title,
            "property_type": parsed.property_type,
            "purpose": parsed.purpose,
            "price": parsed.price,
            "city": parsed.city or raw.metadata_json.get("city"),
            "neighborhood": parsed.neighborhood,
            "address": parsed.address,
            "area_m2": parsed.area_m2,
            "bedrooms": parsed.bedrooms,
            "bathrooms": parsed.bathrooms,
            "parking_spaces": parsed.parking_spaces,
            "condo_fee": parsed.condo_fee,
            "iptu": parsed.iptu,
            "collected_at": raw.collected_at,
        }

    def save_normalized(self, raw: RawCollection, normalized: object | list[object] | None) -> int:
        if not isinstance(normalized, dict):
            return 0
        self.db.add(NormalizedRealEstateListing(raw_collection_id=raw.id, **normalized))
        self.db.flush()
        return 1

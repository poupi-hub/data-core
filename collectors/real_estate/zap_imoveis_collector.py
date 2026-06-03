"""
ZapImoveisCollector — coleta anúncios de imóveis do Zap Imóveis via JSON API.

Usa a Glue API pública do Zap (mesma usada pelo app mobile).
Estratégia: paginação sequencial de resultados de busca.
Salva em raw_collections (module=real_estate, schema=realEstateListing v1.0.0).

Campos coletados: id, título, preço, localização, área, quartos, banheiros,
vagas, tipo, url, atributos, raw_json completo.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from collectors.base import BaseCollector, CollectedItem, CollectorMetadata
from database.models import CollectorDomain

logger = logging.getLogger(__name__)

_GLUE_API = "https://glue-api.zapimoveis.com.br/v2/listings"
_PORTAL = "ZAP"
_PAGE_SIZE = 24
_DEFAULT_MAX_PAGES = 5

_INCLUDE_FIELDS = (
    "search(result(listings(listing(id,title,description,address,"
    "pricingInfos,usableAreas,bedrooms,bathrooms,parkingSpaces,"
    "listingType,unitTypes,propertyType,unitFloor,floors,"
    "amenities,suites,totalAreas),link,medias)),"
    "totalCount)"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "X-Domain": "www.zapimoveis.com.br",
    "Origin": "https://www.zapimoveis.com.br",
    "Referer": "https://www.zapimoveis.com.br/",
}

# Business types: SALE (venda) | RENTAL (aluguel)
_BUSINESS_TYPES = ["SALE", "RENTAL"]

# Localização padrão: São Paulo capital
_DEFAULT_PARAMS_BASE: dict[str, Any] = {
    "user": "user-res-v2",
    "portal": _PORTAL,
    "categoryPage": "RESULT",
    "listingType": "USED",
    "unitTypes": "Apartment,Home,AllotmentLand,Farm,Commercial,Garage,Other",
    "size": str(_PAGE_SIZE),
    "q": "",
    "addressCity": "São Paulo",
    "addressState": "São Paulo",
    "addressCountry": "Brasil",
    "addressZone": "",
    "addressNeighborhood": "",
    "addressStreet": "",
    "addressAccuracy": "2",
    "addressPointLat": "-23.5505199",
    "addressPointLon": "-46.6333094",
    "includeFields": _INCLUDE_FIELDS,
}


def _extract_listing(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten listing structure into a clean payload."""
    listing = raw.get("listing", {}) or {}
    link = raw.get("link", {}) or {}
    medias = raw.get("medias", []) or []

    address = listing.get("address", {}) or {}
    pricing = listing.get("pricingInfos", []) or []
    price_sale = next(
        (p.get("price") for p in pricing if p.get("businessType") == "SALE"), None
    )
    price_rental = next(
        (p.get("monthlyRentPrice") or p.get("price")
         for p in pricing if p.get("businessType") == "RENTAL"), None
    )

    usable_areas = listing.get("usableAreas", []) or []
    total_areas = listing.get("totalAreas", []) or []
    bedrooms = listing.get("bedrooms", []) or []
    bathrooms = listing.get("bathrooms", []) or []
    parking = listing.get("parkingSpaces", []) or []

    listing_url = link.get("href") or ""
    if listing_url and not listing_url.startswith("http"):
        listing_url = f"https://www.zapimoveis.com.br{listing_url}"

    photo_urls = [m.get("value") for m in medias if m.get("value")][:3]

    return {
        "id": listing.get("id"),
        "title": listing.get("title"),
        "description": (listing.get("description") or "")[:500],
        "url": listing_url,
        "price_sale": price_sale,
        "price_rental": price_rental,
        "currency": "BRL",
        "address": {
            "street": address.get("street"),
            "neighborhood": address.get("neighborhood"),
            "city": address.get("city"),
            "state": address.get("state"),
            "zip_code": address.get("zipCode"),
            "lat": address.get("point", {}).get("lat") if address.get("point") else None,
            "lon": address.get("point", {}).get("lon") if address.get("point") else None,
        },
        "area_usable_m2": usable_areas[0] if usable_areas else None,
        "area_total_m2": total_areas[0] if total_areas else None,
        "bedrooms": bedrooms[0] if bedrooms else None,
        "bathrooms": bathrooms[0] if bathrooms else None,
        "parking_spaces": parking[0] if parking else None,
        "listing_type": listing.get("listingType"),
        "property_type": listing.get("propertyType"),
        "unit_types": listing.get("unitTypes", []),
        "amenities": listing.get("amenities", [])[:20],
        "photo_urls": photo_urls,
        "source": "zap_imoveis",
        "raw_listing": raw,  # full raw object preserved
    }


class ZapImoveisCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="real_estate.zap_imoveis",
        domain=CollectorDomain.real_estate,
        source="zap_imoveis",
        description=(
            "Coleta anúncios de imóveis do Zap Imóveis via Glue API. "
            "Cobre venda e aluguel em São Paulo. Raw storage only."
        ),
        default_interval_minutes=360,  # 6h
        collector_version="1.0.0",
        raw_schema_name="realEstateListing",
        raw_schema_version="1.0.0",
        schedulable=True,
    )

    async def collect(self) -> list[CollectedItem]:
        max_pages: int = int(self.config.get("max_pages", _DEFAULT_MAX_PAGES))
        items: list[CollectedItem] = []

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ) as client:
            for business in _BUSINESS_TYPES:
                params = {**_DEFAULT_PARAMS_BASE, "business": business}
                for page in range(max_pages):
                    params["from"] = str(page * _PAGE_SIZE)
                    try:
                        resp = await client.get(_GLUE_API, params=params)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as exc:
                        logger.warning(
                            "ZapImóveis fetch failed",
                            extra={
                                "business": business,
                                "page": page,
                                "error": str(exc),
                            },
                        )
                        break

                    listings_raw = (
                        data.get("search", {})
                        .get("result", {})
                        .get("listings", [])
                        or []
                    )
                    if not listings_raw:
                        break

                    for raw in listings_raw:
                        try:
                            payload = _extract_listing(raw)
                            listing_id = payload.get("id")
                            if not listing_id:
                                continue
                            items.append(
                                CollectedItem(
                                    external_id=f"ZAP-{listing_id}",
                                    source_url=payload.get("url"),
                                    payload=payload,
                                    metadata={
                                        "business_type": business,
                                        "page": page,
                                        "source": "zap_imoveis",
                                    },
                                )
                            )
                        except Exception as exc:
                            logger.debug(
                                "ZapImóveis parse error",
                                extra={"error": str(exc)},
                            )

                    logger.info(
                        "ZapImóveis page collected",
                        extra={
                            "business": business,
                            "page": page,
                            "count": len(listings_raw),
                        },
                    )
                    await asyncio.sleep(1.5)  # polite delay

        logger.info(
            "ZapImóveis collection complete",
            extra={"total_items": len(items)},
        )
        return items

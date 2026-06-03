"""
ImovelWebCollector — coleta anúncios de imóveis do ImovelWeb via scraping HTML.

ImovelWeb é um portal tradicional com HTML estruturado e paginação simples.
Extrai dados de listagens via JSON-LD (schema.org RealEstateListing) e
parsing de cards HTML como fallback.

Salva em raw_collections (module=real_estate, schema=realEstateListing v1.0.0).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector, CollectedItem, CollectorMetadata
from database.models import CollectorDomain

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.imovelweb.com.br"
_DEFAULT_MAX_PAGES = 5

# Páginas de busca: venda e aluguel em São Paulo
_SEARCH_PATHS = [
    ("/imoveis-venda-sao-paulo-sp.html", "SALE"),
    ("/imoveis-aluguel-sao-paulo-sp.html", "RENTAL"),
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://www.imovelweb.com.br/",
}

# ImovelWeb pagination pattern: /imoveis-venda-sao-paulo-sp-pagina-{n}.html
_PAGE_SUFFIX_RE = re.compile(r'(-pagina-\d+)?\.html$')

# JSON-LD types relevant to real estate
_REALESTATE_TYPES = {"RealEstateListing", "Residence", "House", "Apartment"}


def _build_page_url(base_path: str, page: int) -> str:
    if page == 1:
        return f"{_BASE_URL}{base_path}"
    paged = _PAGE_SUFFIX_RE.sub(f"-pagina-{page}.html", base_path)
    return f"{_BASE_URL}{paged}"


def _extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Extract JSON-LD structured data from page."""
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            for item in data:
                if item.get("@type") in _REALESTATE_TYPES:
                    results.append(item)
        elif isinstance(data, dict):
            if data.get("@type") in _REALESTATE_TYPES:
                results.append(data)
            elif data.get("@type") == "ItemList":
                for el in data.get("itemListElement", []):
                    item = el.get("item") or el
                    if isinstance(item, dict) and item.get("@type") in _REALESTATE_TYPES:
                        results.append(item)
    return results


def _extract_from_cards(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Fallback: extract listing data from HTML cards."""
    listings = []
    # ImovelWeb uses data-id on listing articles
    cards = soup.find_all("div", attrs={"data-posting-type": True})
    if not cards:
        cards = soup.find_all("article", class_=re.compile(r"posting-card|property-card"))

    for card in cards:
        try:
            link_tag = card.find("a", href=True)
            url = link_tag["href"] if link_tag else None
            if url and not url.startswith("http"):
                url = f"{_BASE_URL}{url}"

            posting_id = (
                card.get("data-id")
                or card.get("data-posting-id")
                or card.get("id")
            )
            title_tag = card.find("span", class_=re.compile(r"title|heading")) or card.find("h2")
            title = title_tag.get_text(strip=True) if title_tag else None

            price_tag = card.find(class_=re.compile(r"price|preco|valor"))
            price_text = price_tag.get_text(strip=True) if price_tag else None

            location_tag = card.find(class_=re.compile(r"location|localizacao|address"))
            location_text = location_tag.get_text(strip=True) if location_tag else None

            # Parse numeric price from "R$ 850.000"
            price_value: float | None = None
            if price_text:
                digits = re.sub(r"[^\d]", "", price_text)
                if digits and len(digits) >= 4:
                    price_value = float(digits)

            listings.append({
                "id": posting_id,
                "title": title,
                "price": price_value,
                "price_text": price_text,
                "location_text": location_text,
                "url": url,
                "source": "imovelweb",
            })
        except Exception:
            continue

    return listings


def _parse_json_ld_listing(item: dict[str, Any]) -> dict[str, Any]:
    """Map JSON-LD RealEstateListing → our standard payload."""
    address = item.get("address", {}) or {}
    offers = item.get("offers", {}) or {}
    geo = item.get("geo", {}) or {}
    price = offers.get("price") or item.get("price")

    return {
        "id": item.get("identifier") or item.get("@id"),
        "title": item.get("name") or item.get("headline"),
        "description": (item.get("description") or "")[:500],
        "url": item.get("url"),
        "price": float(price) if price else None,
        "price_currency": offers.get("priceCurrency") or "BRL",
        "currency": "BRL",
        "address": {
            "street": address.get("streetAddress"),
            "neighborhood": address.get("addressLocality"),
            "city": address.get("addressRegion"),
            "state": address.get("addressCountry"),
            "zip_code": address.get("postalCode"),
            "lat": geo.get("latitude"),
            "lon": geo.get("longitude"),
        },
        "area_m2": item.get("floorSize", {}).get("value") if isinstance(item.get("floorSize"), dict) else item.get("floorSize"),
        "number_of_rooms": item.get("numberOfRooms"),
        "number_of_bathrooms": item.get("numberOfBathroomsTotal"),
        "source": "imovelweb",
        "property_type": item.get("@type"),
        "photo_urls": [
            (img.get("url") or img if isinstance(img, str) else None)
            for img in (item.get("image") or [])[:3]
            if img
        ],
        "raw_json_ld": item,
    }


class ImovelWebCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="real_estate.imovelweb",
        domain=CollectorDomain.real_estate,
        source="imovelweb",
        description=(
            "Coleta anúncios de imóveis do ImovelWeb via scraping HTML. "
            "Extrai JSON-LD (schema.org) com fallback para parsing de cards. "
            "Cobre venda e aluguel em São Paulo. Raw storage only."
        ),
        default_interval_minutes=480,  # 8h
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
            for search_path, business in _SEARCH_PATHS:
                for page in range(1, max_pages + 1):
                    url = _build_page_url(search_path, page)
                    try:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        html = resp.text
                    except Exception as exc:
                        logger.warning(
                            "ImovelWeb fetch failed",
                            extra={"url": url, "page": page, "error": str(exc)},
                        )
                        break

                    soup = BeautifulSoup(html, "html.parser")

                    # Try JSON-LD first (structured, reliable)
                    json_ld_listings = _extract_json_ld(soup)
                    raw_listings: list[dict[str, Any]] = []

                    if json_ld_listings:
                        for item_data in json_ld_listings:
                            try:
                                raw_listings.append(_parse_json_ld_listing(item_data))
                            except Exception as exc:
                                logger.debug("ImovelWeb JSON-LD parse error", extra={"error": str(exc)})
                    else:
                        raw_listings = _extract_from_cards(soup)

                    if not raw_listings:
                        logger.info(
                            "ImovelWeb no listings found",
                            extra={"url": url, "page": page},
                        )
                        break

                    for listing in raw_listings:
                        try:
                            listing_id = listing.get("id")
                            external_id = f"IW-{listing_id}" if listing_id else f"IW-p{page}-{hash(listing.get('url', ''))}"
                            items.append(
                                CollectedItem(
                                    external_id=external_id,
                                    source_url=listing.get("url"),
                                    payload=listing,
                                    metadata={
                                        "business_type": business,
                                        "page": page,
                                        "source": "imovelweb",
                                    },
                                )
                            )
                        except Exception as exc:
                            logger.debug("ImovelWeb item error", extra={"error": str(exc)})

                    logger.info(
                        "ImovelWeb page collected",
                        extra={"url": url, "page": page, "count": len(raw_listings)},
                    )
                    await asyncio.sleep(2.0)

        logger.info("ImovelWeb collection complete", extra={"total_items": len(items)})
        return items

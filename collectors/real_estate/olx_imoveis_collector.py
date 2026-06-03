"""
OLXImoveisCollector — coleta anúncios de imóveis da OLX via scraping de páginas de busca.

Estratégia: fetch de páginas HTML de resultado de busca, extração de __NEXT_DATA__
(OLX usa Next.js) com fallback para parsing HTML via BeautifulSoup.

Salva em raw_collections (module=real_estate, schema=realEstateListing v1.0.0).

Campos coletados: id, título, preço, localização, atributos, url, raw_json.
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

_BASE_URL = "https://www.olx.com.br"
_DEFAULT_MAX_PAGES = 5

# Categorias de imóveis na OLX: 1020=Imóveis, subcat 1 = Venda, subcat 2 = Aluguel
_SEARCH_URLS = [
    "/imoveis/venda/estado-sp",
    "/imoveis/aluguel/estado-sp",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Referer": "https://www.olx.com.br/",
}

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)


def _extract_from_next_data(html: str) -> list[dict[str, Any]]:
    """Extract listings from __NEXT_DATA__ JSON (Next.js)."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return []
    try:
        next_data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    # Navigate Next.js page props to find ad list
    props = next_data.get("props", {})
    page_props = props.get("pageProps", {})

    # OLX stores ads in multiple possible paths
    ads: list[dict[str, Any]] = []

    # Path 1: pageProps.ads
    if "ads" in page_props:
        ads = page_props["ads"]

    # Path 2: pageProps.data.ads
    elif "data" in page_props and "ads" in (page_props.get("data") or {}):
        ads = page_props["data"]["ads"]

    # Path 3: pageProps.listings
    elif "listings" in page_props:
        ads = page_props["listings"]

    return ads if isinstance(ads, list) else []


def _extract_from_html(html: str, base_url: str) -> list[dict[str, Any]]:
    """HTML fallback: extract basic listing data from OLX listing cards."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # OLX listing cards have data-lurker-detail or class containing listing info
    cards = soup.find_all("li", attrs={"data-lurker-detail": True})
    if not cards:
        # Try alternate selectors
        cards = soup.find_all("li", class_=re.compile(r"fnmrjs-\d+|sc-1fcmfeb"))

    for card in cards:
        try:
            link_tag = card.find("a", href=True)
            if not link_tag:
                continue
            url = link_tag["href"]
            if url.startswith("/"):
                url = f"{base_url}{url}"

            title_tag = card.find("h2") or card.find("h3")
            title = title_tag.get_text(strip=True) if title_tag else None

            price_tag = card.find(attrs={"data-testid": "ad-card-price"}) or \
                        card.find(class_=re.compile(r"price|preco"))
            price_text = price_tag.get_text(strip=True) if price_tag else None

            listings.append({
                "id": None,
                "title": title,
                "price_text": price_text,
                "url": url,
                "source": "olx_imoveis",
            })
        except Exception:
            continue

    return listings


def _parse_ad(ad: dict[str, Any]) -> dict[str, Any]:
    """Normalize an OLX ad dict into our standard schema."""
    location = ad.get("location", {}) or {}
    images = ad.get("images", []) or ad.get("medias", []) or []
    price = ad.get("price")
    if isinstance(price, dict):
        price_value = price.get("value") or price.get("cents")
        if price_value and str(price_value).isdigit() and int(str(price_value)) > 100000:
            # possibly in cents
            price_value = int(price_value) / 100
    else:
        price_value = price

    return {
        "id": ad.get("id") or ad.get("listId") or ad.get("list_id"),
        "title": ad.get("title") or ad.get("subject"),
        "description": (ad.get("body") or ad.get("description") or "")[:500],
        "url": ad.get("url") or ad.get("original_url") or ad.get("href"),
        "price": price_value,
        "price_text": ad.get("priceValue") or ad.get("price_text"),
        "currency": "BRL",
        "address": {
            "city": location.get("city") or location.get("municipality"),
            "state": location.get("state") or location.get("uf"),
            "neighborhood": location.get("neighbourhood") or location.get("neighborhood"),
            "zip_code": location.get("zipcode") or location.get("cep"),
        },
        "photo_urls": [
            img.get("original") or img.get("url") or img.get("value")
            for img in images
            if (img.get("original") or img.get("url") or img.get("value"))
        ][:3],
        "source": "olx_imoveis",
        "category": ad.get("category") or ad.get("categoryValue"),
        "attributes": ad.get("properties") or ad.get("params") or [],
        "published_at": ad.get("date") or ad.get("listTime"),
        "raw_ad": {k: v for k, v in ad.items() if k not in ("body", "description")},
    }


class OLXImoveisCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="real_estate.olx_imoveis",
        domain=CollectorDomain.real_estate,
        source="olx_imoveis",
        description=(
            "Coleta anúncios de imóveis da OLX via scraping de páginas de busca. "
            "Extrai __NEXT_DATA__ (Next.js) com fallback HTML. "
            "Cobre venda e aluguel em SP. Raw storage only."
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
            for search_path in _SEARCH_URLS:
                business = "SALE" if "venda" in search_path else "RENTAL"
                for page in range(1, max_pages + 1):
                    url = f"{_BASE_URL}{search_path}"
                    params: dict[str, Any] = {}
                    if page > 1:
                        params["o"] = str(page)

                    try:
                        resp = await client.get(url, params=params)
                        resp.raise_for_status()
                        html = resp.text
                    except Exception as exc:
                        logger.warning(
                            "OLX fetch failed",
                            extra={"path": search_path, "page": page, "error": str(exc)},
                        )
                        break

                    ads = _extract_from_next_data(html)
                    if not ads:
                        ads = _extract_from_html(html, _BASE_URL)

                    if not ads:
                        logger.info(
                            "OLX no listings found",
                            extra={"path": search_path, "page": page},
                        )
                        break

                    for ad in ads:
                        try:
                            payload = _parse_ad(ad)
                            ad_id = payload.get("id")
                            external_id = f"OLX-{ad_id}" if ad_id else f"OLX-p{page}-{hash(payload.get('url', ''))}"
                            items.append(
                                CollectedItem(
                                    external_id=external_id,
                                    source_url=payload.get("url"),
                                    payload=payload,
                                    metadata={
                                        "business_type": business,
                                        "page": page,
                                        "source": "olx_imoveis",
                                    },
                                )
                            )
                        except Exception as exc:
                            logger.debug("OLX parse error", extra={"error": str(exc)})

                    logger.info(
                        "OLX page collected",
                        extra={"path": search_path, "page": page, "count": len(ads)},
                    )
                    await asyncio.sleep(2.0)

        logger.info("OLX collection complete", extra={"total_items": len(items)})
        return items

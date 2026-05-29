"""
EcommerceURLScraper — Python-native scraper for VTEX-based Brazilian e-commerce stores.

Replaces the TypeScript PoupiLegacyRawCollector dependency.
Supports: Drogasil, Drogaraia, Pague Menos, and any VTEX-based store.

Strategies (in order):
  1. VTEX Catalog API — when product ID is extractable from URL (most reliable)
  2. JSON-LD structured data — universal fallback for any store
  3. Records failure payload if both fail

Output schema: scrapedProduct v1.0.0
  Compatible with: PoupiLegacyScrapedProductV1Normalizer
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from api.metrics import (
    collection_attempts_total,
    collection_duration_seconds,
    collection_empty_total,
    collection_errors_total,
    collection_failed_total,
    collection_raw_duplicates_total,
    collection_raw_saved_total,
    collection_success_total,
    collector_last_failure_timestamp,
    collector_last_success_timestamp,
    scraper_anti_bot_detections_total,
    scraper_drift_events_total,
    scraper_drift_risk,
    scraper_fallback_depth_total,
    scraper_quality_score,
)
from app.raw.service import RawCollectionService
from app.scrapers.anti_bot import AntiBotDetector
from app.scrapers.quality import PayloadQualityScorer
from database.models import CollectionRun, CollectorError, RunStatus
from database.session import SessionLocal

logger = logging.getLogger(__name__)

# User-agent that passes basic bot checks on VTEX stores
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

_JSON_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# VTEX stores: domain suffix → store_name (used when source_name not set)
VTEX_STORES: dict[str, str] = {
    "drogasil.com.br": "drogasil",
    "drogaraia.com.br": "drogaraia",
    "paguemenos.com.br": "paguemenos",
    "nissei.com.br": "nissei",
    "ultrafarma.com.br": "ultrafarma",
    "panvel.com": "panvel",
    "drogariasaopaulo.com.br": "drogariasaopaulo",
    "drogariaspacheco.com.br": "drogariaspacheco",
    "consultaremedios.com.br": "consultaremedios",
    "farma22.com.br": "farma22",
}

# Regex to extract VTEX product ID from URL  (e.g. /slug-1351898.html or /p/1351898)
_VTEX_ID_RE = re.compile(r"-(\d{4,})\.html$|/p/(\d{4,})$", re.IGNORECASE)


class EcommerceURLScraper:
    """
    Python URL scraper for VTEX-based Brazilian e-commerce stores.

    Not a BaseCollector subclass — follows PoupiLegacyRawCollector pattern:
    receives targets at instantiation and saves raw directly via RawCollectionService.

    collector_name used in collection_targets: "ecommerce.url_scraper"
    """

    module = "ecommerce"
    collector_name = "ecommerce.url_scraper"
    collector_version = "1.0.0"
    raw_schema_name = "scrapedProduct"
    raw_schema_version = "1.0.0"
    source_type = "python_url_scraper"

    def __init__(
        self,
        db: Session,
        *,
        timeout_seconds: int = 30,
        retry_attempts: int = 2,
        retry_backoff_seconds: float = 3.0,
        delay_seconds: float = 0.5,
    ) -> None:
        self.db = db
        self.timeout = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff_seconds
        self.delay_seconds = delay_seconds
        self._raw_service = RawCollectionService(db)
        self._quality_scorer = PayloadQualityScorer()
        self._anti_bot_detector = AntiBotDetector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect_targets(self, targets: list[Any]) -> dict[str, int]:
        """Synchronous entry point — dispatches to the shared persistent event loop.

        Uses ``scheduler.async_runner.run_async`` instead of ``asyncio.run()`` so
        that repeated scraper calls (every 2 h from APScheduler) reuse the same
        event loop rather than creating and leaking a new one each time.
        """
        from scheduler.async_runner import run_async  # local import to keep collectors/ decoupled
        return run_async(self._collect_all(targets))

    # ------------------------------------------------------------------
    # Async collection
    # ------------------------------------------------------------------

    async def _collect_all(self, targets: list[Any]) -> dict[str, int]:
        raw_saved = 0
        errors = 0
        _domain = "ecommerce"
        _labels = {"domain": _domain, "collector_name": self.collector_name}

        start = time.perf_counter()
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            limits=limits,
        ) as client:
            for target in targets:
                collection_attempts_total.labels(**_labels).inc()
                target_start = time.perf_counter()
                try:
                    product, anti_bot_result = await self._scrape_with_retry(
                        client, target.target_url, target.source_name
                    )
                    target_latency = time.perf_counter() - target_start

                    # ── Anti-bot metrics ──────────────────────────────────────
                    if anti_bot_result and anti_bot_result.detected:
                        scraper_anti_bot_detections_total.labels(
                            source_name=target.source_name,
                            detection_type=anti_bot_result.detection_type,
                        ).inc()
                        logger.warning(
                            "Anti-bot detected",
                            extra={
                                "url": target.target_url,
                                "source": target.source_name,
                                "detection_type": anti_bot_result.detection_type,
                                "confidence": anti_bot_result.confidence,
                            },
                        )

                    if product and product.get("success"):
                        collection_success_total.labels(**_labels).inc()
                        scraped = product.get("scrapedProduct", {})
                        strategy = scraped.get("scraper_strategy", "unknown")

                        # ── Strategy / fallback depth metrics ─────────────────
                        scraper_fallback_depth_total.labels(
                            source_name=target.source_name,
                            strategy=strategy,
                        ).inc()

                        # ── Quality scoring ───────────────────────────────────
                        quality = self._quality_scorer.score(scraped, latency_seconds=target_latency)
                        scraper_quality_score.labels(
                            source_name=target.source_name,
                            strategy=strategy,
                        ).observe(quality.score)
                        if not quality.is_acceptable:
                            logger.warning(
                                "Low payload quality",
                                extra={
                                    "url": target.target_url,
                                    "source": target.source_name,
                                    "quality_score": quality.score,
                                    "issues": quality.issues,
                                },
                            )

                        was_saved = self._save_product(
                            target, product,
                            quality_dict=quality.to_dict(),
                            anti_bot_detected=anti_bot_result.detected if anti_bot_result else False,
                        )
                        if was_saved:
                            raw_saved += 1
                    else:
                        collection_empty_total.labels(**_labels).inc()
                        errors += 1
                        self._save_product(
                            target, product,
                            quality_dict=None,
                            anti_bot_detected=anti_bot_result.detected if anti_bot_result else False,
                        )
                except Exception:
                    logger.exception(
                        "Scrape failed",
                        extra={"url": target.target_url, "source": target.source_name},
                    )
                    collection_failed_total.labels(**_labels).inc()
                    errors += 1

                if self.delay_seconds > 0:
                    await asyncio.sleep(self.delay_seconds)

        elapsed = time.perf_counter() - start
        collection_duration_seconds.labels(**_labels).observe(elapsed)

        now = time.time()
        if errors == 0 or raw_saved > 0:
            collector_last_success_timestamp.labels(**_labels).set(now)
        if errors > 0 and raw_saved == 0:
            collector_last_failure_timestamp.labels(**_labels).set(now)

        return {"raw_saved_count": raw_saved, "error_count": errors}

    async def _scrape_with_retry(
        self, client: httpx.AsyncClient, url: str, source_name: str
    ) -> tuple[dict[str, Any], Any]:
        """Return (product_dict, anti_bot_result). Never raises."""
        from app.scrapers.anti_bot import AntiBotResult
        last_exc: Exception | None = None
        last_anti_bot = None

        for attempt in range(1, self.retry_attempts + 1):
            try:
                product, anti_bot = await self._scrape_url(client, url, source_name)
                last_anti_bot = anti_bot
                if anti_bot and anti_bot.detected:
                    # Don't retry on hard blocks — no point hammering the site
                    if anti_bot.detection_type in ("cloudflare", "captcha"):
                        return product, anti_bot
                return product, anti_bot
            except Exception as exc:
                last_exc = exc
                if attempt < self.retry_attempts:
                    wait = self.retry_backoff * attempt
                    logger.warning(
                        "Scrape attempt %d failed, retrying in %.1fs",
                        attempt,
                        wait,
                        extra={"url": url},
                    )
                    await asyncio.sleep(wait)

        # All retries failed — return failure payload instead of raising
        logger.error("All scrape attempts failed", extra={"url": url, "error": str(last_exc)})
        error_message = str(last_exc)
        return {
            "success": False,
            "error": _classify_error_message(error_message),
            "error_message": error_message,
        }, last_anti_bot

    async def _scrape_url(
        self, client: httpx.AsyncClient, url: str, source_name: str
    ) -> tuple[dict[str, Any], Any]:
        """Try VTEX API first, then JSON-LD.  Returns (product, anti_bot_result)."""
        from app.scrapers.anti_bot import AntiBotDetector as _ABD
        anti_bot = None
        product_id = _extract_vtex_product_id(url)
        store_name = source_name or _guess_store_name(url)

        # Strategy 1: VTEX Catalog API (only when product ID extractable)
        if product_id:
            try:
                result, raw_resp = await self._fetch_vtex_api(client, url, product_id, store_name)
                if raw_resp is not None:
                    # For JSON API endpoints only check HTTP status (not body size / honeypot)
                    status = getattr(raw_resp, "status_code", 200)
                    if status in (403, 429):
                        anti_bot = self._anti_bot_detector.from_raw(
                            status_code=status, body="", url=url
                        )
                        return {"success": False, "error_message": f"anti_bot:{anti_bot.detection_type}"}, anti_bot
                if result and result.get("price") is not None:
                    logger.debug("VTEX API success", extra={"url": url, "product_id": product_id})
                    return {"success": True, "scrapedProduct": result}, anti_bot
            except Exception as exc:
                logger.debug("VTEX API failed, falling back to JSON-LD: %s", exc)

        # Strategy 2: JSON-LD from HTML — full anti-bot detection (body + status)
        result, raw_resp = await self._fetch_jsonld(client, url, store_name)
        if raw_resp is not None:
            anti_bot = self._anti_bot_detector.from_response(raw_resp, url=url)
            if anti_bot.detected:
                return {"success": False, "error_message": f"anti_bot:{anti_bot.detection_type}"}, anti_bot
        if result and result.get("price") is not None:
            logger.debug("JSON-LD success", extra={"url": url})
            return {"success": True, "scrapedProduct": result}, anti_bot

        # Both failed
        return {"success": False, "error_message": "Could not extract product data"}, anti_bot

    # ------------------------------------------------------------------
    # Strategy 1: VTEX Catalog API
    # ------------------------------------------------------------------

    async def _fetch_vtex_api(
        self,
        client: httpx.AsyncClient,
        page_url: str,
        product_id: str,
        store_name: str,
    ) -> tuple[dict[str, Any] | None, Any]:
        """Returns (product_dict_or_None, raw_httpx_response)."""
        parsed = urlparse(page_url)
        api_url = f"{parsed.scheme}://{parsed.netloc}/api/catalog_system/pub/products/search?fq=productId:{product_id}"

        headers = {**_JSON_HEADERS, "Referer": page_url}
        resp = await client.get(api_url, headers=headers)
        resp.raise_for_status()

        data: list[dict] = resp.json()
        if not data:
            return None, resp

        product = data[0]
        items = product.get("items", [])
        item = items[0] if items else {}
        sellers = item.get("sellers", [])
        seller = sellers[0] if sellers else {}
        offer = seller.get("commertialOffer", {})

        price = offer.get("Price") or offer.get("SellingPrice")
        list_price = offer.get("ListPrice")
        available_qty = offer.get("AvailableQuantity", 0)

        images = item.get("images", [])
        image_url = images[0].get("imageUrl") if images else None

        return {
            "title": product.get("productName", ""),
            "brand": product.get("brand", ""),
            "price": float(price) if price is not None else None,
            "list_price": float(list_price) if list_price else None,
            "availability": "in_stock" if int(available_qty) > 0 else "out_of_stock",
            "source_id": str(product.get("productId", product_id)),
            "store_name": store_name,
            "url": page_url,
            "currency": "BRL",
            "image_url": image_url,
            "ean": item.get("ean", ""),
            "scraper_strategy": "vtex_api",
        }, resp

    # ------------------------------------------------------------------
    # Strategy 2: JSON-LD structured data
    # ------------------------------------------------------------------

    async def _fetch_jsonld(
        self,
        client: httpx.AsyncClient,
        url: str,
        store_name: str,
    ) -> tuple[dict[str, Any] | None, Any]:
        """Returns (product_dict_or_None, raw_httpx_response)."""
        headers = {**_DEFAULT_HEADERS, "Referer": f"https://{urlparse(url).netloc}/"}
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = script.string
                if not raw or "Product" not in raw:
                    continue
                data = json.loads(raw)

                # Handle array of schemas
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") == "Product"), None)
                if not data or data.get("@type") != "Product":
                    continue

                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}

                # AggregateOffer uses lowPrice/highPrice; plain Offer uses price
                raw_price = (
                    offers.get("price")
                    or offers.get("lowPrice")
                    or offers.get("highPrice")
                )
                price = _parse_ld_price(raw_price)
                # For AggregateOffer the availability lives inside sub-offers, not on the root.
                avail_url = offers.get("availability", "")
                if not avail_url and offers.get("@type") == "AggregateOffer":
                    sub_offers = offers.get("offers", [])
                    if isinstance(sub_offers, list) and sub_offers:
                        avail_url = sub_offers[0].get("availability", "")
                    elif isinstance(sub_offers, dict):
                        avail_url = sub_offers.get("availability", "")
                availability = "in_stock" if "InStock" in avail_url else "out_of_stock"

                brand = data.get("brand", {})
                brand_name = brand.get("name", "") if isinstance(brand, dict) else str(brand)

                # Try to extract source_id from sku, productID, or URL
                source_id = (
                    str(data.get("sku") or data.get("productID") or "")
                    or _extract_vtex_product_id(url)
                    or ""
                )

                return {
                    "title": data.get("name", "") or _extract_og_title(soup),
                    "brand": brand_name,
                    "price": price,
                    "availability": availability,
                    "source_id": source_id,
                    "store_name": store_name,
                    "url": url,
                    "currency": offers.get("priceCurrency", "BRL"),
                    "scraper_strategy": "json_ld",
                }, resp
            except (json.JSONDecodeError, AttributeError, KeyError):
                continue

        return None, resp

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_product(
        self,
        target: Any,
        product: dict[str, Any],
        quality_dict: dict | None = None,
        anti_bot_detected: bool = False,
    ) -> bool:
        """Save product payload as a raw collection record.

        Returns True when a new row was inserted, False on duplicate or error.
        Stores quality score and anti-bot flag in metadata_json for diagnostics.
        """
        scraped = product.get("scrapedProduct", {})
        source_id = scraped.get("source_id") if product.get("success") else None
        strategy = scraped.get("scraper_strategy", "unknown")
        _labels = {"domain": "ecommerce", "collector_name": self.collector_name}

        metadata: dict = {
            "collection_target_id": str(target.id),
            "strategy": strategy,
            "anti_bot_detected": anti_bot_detected,
        }
        if not product.get("success"):
            metadata["error_type"] = _classify_error_message(
                product.get("error") or product.get("error_message")
            )
        if quality_dict is not None:
            metadata["quality"] = quality_dict

        raw_payload = {
            **product,
            "collection_attempted_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            saved = self._raw_service.save_json(
                module=self.module,
                source_name=target.source_name,
                source_type=self.source_type,
                collector_name=self.collector_name,
                collector_version=self.collector_version,
                raw_schema_name=self.raw_schema_name,
                raw_schema_version=self.raw_schema_version,
                source_id=source_id,
                target_url=target.target_url,
                raw_json=raw_payload,
                metadata=metadata,
            )
            was_new = getattr(saved, "_raw_was_created", True)
            if was_new:
                collection_raw_saved_total.labels(**_labels).inc()
            else:
                collection_raw_duplicates_total.labels(**_labels).inc()
            return was_new
        except Exception:
            logger.exception(
                "Failed to save raw product",
                extra={"url": target.target_url, "source": target.source_name},
            )
            collection_errors_total.labels(**_labels, error_type="SaveError").inc()
            return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _extract_vtex_product_id(url: str) -> str | None:
    """Extract VTEX numeric product ID from URL."""
    m = _VTEX_ID_RE.search(url)
    if m:
        return m.group(1) or m.group(2)
    return None


def _guess_store_name(url: str) -> str:
    """Guess store name from domain."""
    host = urlparse(url).netloc.lower().lstrip("www.")
    for domain, name in VTEX_STORES.items():
        if host.endswith(domain):
            return name
    # Fallback: first part of domain
    return host.split(".")[0]


def _classify_error_message(error: Any) -> str:
    text = str(error or "").strip()
    normalized = text.lower()
    if "403 forbidden" in normalized or "status/403" in normalized:
        return "HTTP_403_FORBIDDEN"
    if "429" in normalized or "too many requests" in normalized:
        return "HTTP_429_RATE_LIMIT"
    if "timeout" in normalized or "timed out" in normalized:
        return "TIMEOUT"
    if normalized.startswith("anti_bot:") or "captcha" in normalized or "cloudflare" in normalized:
        return text
    if "could not extract product data" in normalized:
        return "PARSE_FAILURE"
    if "selector" in normalized:
        return "SELECTOR_FAILURE"
    return text or "unknown_error"


def _parse_ld_price(raw: Any) -> float | None:
    """Parse price from JSON-LD offers.price.

    Handles international format ("99.90"), BR format ("1.299,90"),
    and numeric types directly.
    """
    if raw is None:
        return None
    try:
        s = str(raw).strip().replace("R$", "").replace("\xa0", "").replace(" ", "")
        if not s:
            return None
        # BR format: comma is the decimal separator (e.g. "1.299,90" or "99,90")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        return float(s)
    except (ValueError, TypeError):
        return None


def _extract_og_title(soup: BeautifulSoup) -> str:
    """Fallback: extract product title from Open Graph meta tags."""
    tag = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "title"})
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""

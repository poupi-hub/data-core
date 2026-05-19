"""Tests for EcommerceURLScraper.

Mocks httpx.AsyncClient so no live HTTP requests are made.
Uses db_session fixture (requires PostgreSQL) — skipped automatically if unavailable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collectors.ecommerce.url_scraper import (
    EcommerceURLScraper,
    _extract_vtex_product_id,
    _guess_store_name,
    _parse_ld_price,
)
from app.raw.models import RawCollection


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def _make_target(
    url: str = "https://www.drogasil.com.br/produto-1351898.html",
    source_name: str = "drogasil",
    target_id: str = "00000000-0000-0000-0000-000000000001",
):
    t = SimpleNamespace()
    t.target_url = url
    t.source_name = source_name
    t.id = target_id
    return t


def _vtex_api_response(product_id: str = "1351898", price: float = 99.90) -> dict:
    return [
        {
            "productId": product_id,
            "productName": "Fralda Pampers Confort Sec",
            "brand": "Pampers",
            "items": [
                {
                    "ean": "7500435131773",
                    "images": [{"imageUrl": "https://drogasil.com.br/img/123.jpg"}],
                    "sellers": [
                        {
                            "commertialOffer": {
                                "Price": price,
                                "ListPrice": price + 10.0,
                                "AvailableQuantity": 100,
                            }
                        }
                    ],
                }
            ],
        }
    ]


def _jsonld_html(name: str = "Fralda Pampers", price: float = 79.90) -> str:
    ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "sku": "1351898",
        "brand": {"@type": "Brand", "name": "Pampers"},
        "offers": {
            "@type": "Offer",
            "price": str(price),
            "priceCurrency": "BRL",
            "availability": "https://schema.org/InStock",
        },
    }
    # Pad with realistic page content so anti-bot detector doesn't flag as honeypot
    padding = "<!-- page content -->\n" + "<p>Produto em estoque. Compre com frete grátis.</p>\n" * 80
    return (
        f'<html><head>'
        f'<title>{name}</title>'
        f'<meta name="description" content="Compre {name} com desconto.">'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        f'</head><body>{padding}</body></html>'
    )


def _make_http_response(body: str | bytes | dict | list, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    if isinstance(body, (dict, list)):
        resp.json.return_value = body
        resp.text = json.dumps(body)
    elif isinstance(body, bytes):
        resp.text = body.decode()
        resp.json.return_value = json.loads(resp.text) if resp.text.startswith(("[", "{")) else {}
    else:
        resp.text = body
        resp.json.return_value = json.loads(resp.text) if resp.text.startswith(("[", "{")) else {}
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Unit: helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.drogasil.com.br/fralda-pampers-1351898.html", "1351898"),
    ("https://paguemenos.com.br/produto/p/9876543", "9876543"),
    ("https://drogasil.com.br/sem-id.html", None),
])
def test_extract_vtex_product_id(url: str, expected: str | None):
    assert _extract_vtex_product_id(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://www.drogasil.com.br/produto/1", "drogasil"),
    ("https://www.drogaraia.com.br/produto/2", "drogaraia"),
    ("https://panvel.com/produto/3", "panvel"),
    ("https://meusite.com.br/produto/4", "meusite"),
])
def test_guess_store_name(url: str, expected: str):
    assert _guess_store_name(url) == expected


@pytest.mark.parametrize("raw,expected", [
    ("99.90", 99.90),
    ("1.299,90", 1299.90),
    (99.9, 99.90),
    (None, None),
    ("invalid", None),
])
def test_parse_ld_price(raw, expected):
    assert _parse_ld_price(raw) == expected


# ---------------------------------------------------------------------------
# Integration: collect_targets with mocked httpx
# ---------------------------------------------------------------------------

@pytest.fixture()
def scraper(db_session):
    return EcommerceURLScraper(
        db_session,
        timeout_seconds=5,
        retry_attempts=1,
        retry_backoff_seconds=0.0,
        delay_seconds=0.0,
    )


def test_collect_targets_vtex_api_saves_raw(db_session, scraper):
    """VTEX API strategy: product data persisted to raw_collections."""
    target = _make_target()
    vtex_resp = _make_http_response(_vtex_api_response())

    async def _get(url, **kwargs):
        return vtex_resp

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        result = scraper.collect_targets([target])
        db_session.commit()

    assert result["raw_saved_count"] == 1
    assert result["error_count"] == 0

    row = (
        db_session.query(RawCollection)
        .filter(
            RawCollection.source_name == "drogasil",
            RawCollection.target_url == target.target_url,
        )
        .order_by(RawCollection.collected_at.desc())
        .first()
    )
    assert row is not None
    assert row.raw_json["success"] is True
    assert row.raw_json["scrapedProduct"]["price"] == 99.90


def test_collect_targets_jsonld_fallback_saves_raw(db_session, scraper):
    """JSON-LD fallback: when VTEX API fails, HTML JSON-LD is used."""
    target = _make_target(
        url="https://www.drogasil.com.br/produto-sem-id-numerico.html",
        source_name="drogasil",
    )
    html_resp = _make_http_response(_jsonld_html(price=79.90))
    html_resp.raise_for_status = MagicMock()

    async def _get(url, **kwargs):
        return html_resp

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        result = scraper.collect_targets([target])
        db_session.commit()

    assert result["raw_saved_count"] == 1
    row = (
        db_session.query(RawCollection)
        .filter(
            RawCollection.source_name == "drogasil",
            RawCollection.target_url == target.target_url,
        )
        .order_by(RawCollection.collected_at.desc())
        .first()
    )
    assert row is not None
    assert row.raw_json["scrapedProduct"]["price"] == 79.90
    assert row.raw_json["scrapedProduct"]["scraper_strategy"] == "json_ld"


def test_collect_targets_deduplicates_identical_payload(db_session, scraper):
    """Saving identical payload twice does not raise and returns error_count=0."""
    target = _make_target()
    vtex_resp = _make_http_response(_vtex_api_response())

    async def _get(url, **kwargs):
        return vtex_resp

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        r1 = scraper.collect_targets([target])
        db_session.commit()
        r2 = scraper.collect_targets([target])
        db_session.commit()

    assert r1["raw_saved_count"] == 1
    assert r2["raw_saved_count"] == 0  # duplicate — not saved again
    assert r1["error_count"] == 0
    assert r2["error_count"] == 0


def test_collect_targets_scrape_failure_returns_error_count(db_session, scraper):
    """HTTP error on scrape: error_count incremented, raw_saved_count=0."""
    import httpx as _httpx
    target = _make_target()

    async def _get(url, **kwargs):
        raise _httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock(status_code=403))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        result = scraper.collect_targets([target])

    assert result["raw_saved_count"] == 0
    assert result["error_count"] == 1


def test_collect_targets_multiple_sources(db_session, scraper):
    """Two targets from different sources both saved correctly."""
    targets = [
        _make_target("https://www.drogasil.com.br/p-1111111.html", "drogasil", "00000000-0000-0000-0000-000000000001"),
        _make_target("https://www.drogaraia.com.br/p-2222222.html", "drogaraia", "00000000-0000-0000-0000-000000000002"),
    ]

    responses = [
        _make_http_response(_vtex_api_response("1111111", 49.90)),
        _make_http_response(_vtex_api_response("2222222", 55.00)),
    ]
    response_iter = iter(responses)

    async def _get(url, **kwargs):
        return next(response_iter)

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        result = scraper.collect_targets(targets)
        db_session.commit()

    assert result["raw_saved_count"] == 2
    assert result["error_count"] == 0


def test_collect_targets_partial_failure(db_session, scraper):
    """First target succeeds, second fails: partial result tracked correctly."""
    import httpx as _httpx
    targets = [
        _make_target("https://www.drogasil.com.br/produto-1351898.html", "drogasil"),
        _make_target("https://www.drogaraia.com.br/produto-9999999.html", "drogaraia"),
    ]

    ok_resp = _make_http_response(_vtex_api_response())
    call_count = {"n": 0}

    async def _get(url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ok_resp
        raise _httpx.ConnectTimeout("timeout")

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        result = scraper.collect_targets(targets)
        db_session.commit()

    assert result["raw_saved_count"] == 1
    assert result["error_count"] == 1


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

def test_prometheus_collection_raw_saved_total_increments(db_session, scraper):
    """collection_raw_saved_total increments when a new raw record is created."""
    from api.metrics import collection_raw_saved_total

    target = _make_target(
        url="https://www.paguemenos.com.br/fralda-pampers-p-7654321.html",
        source_name="paguemenos",
        target_id="00000000-0000-0000-0000-000000000099",
    )

    before = collection_raw_saved_total.labels(
        domain="ecommerce", collector_name="ecommerce.url_scraper"
    )._value.get()

    vtex_resp = _make_http_response(_vtex_api_response("7654321", 88.0))

    async def _get(url, **kwargs):
        return vtex_resp

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        scraper.collect_targets([target])
        db_session.commit()

    after = collection_raw_saved_total.labels(
        domain="ecommerce", collector_name="ecommerce.url_scraper"
    )._value.get()

    assert after == before + 1

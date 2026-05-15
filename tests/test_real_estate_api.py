"""
test_real_estate_api.py

Integration tests for GET /api/v1/real-estate/* endpoints.
Uses a real database (db_session fixture) seeded with minimal records,
and the FastAPI TestClient (api_client fixture) for HTTP assertions.
"""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.modules.real_estate.models import RealEstateListing, RealEstateSource


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_source(db: Session, *, city: str = "São Paulo") -> RealEstateSource:
    source = RealEstateSource(
        id=str(uuid4()),
        name=f"pytest-source-{uuid4().hex[:6]}",
        base_url="https://example.com",
        city=city,
        state="SP",
        active=True,
    )
    db.add(source)
    db.flush()
    return source


def _make_listing(db: Session, source: RealEstateSource, *, city: str = "São Paulo") -> RealEstateListing:
    listing = RealEstateListing(
        id=str(uuid4()),
        source_id=source.id,
        url=f"https://example.com/imovel/{uuid4().hex[:8]}",
        title="Apartamento 2 quartos",
        property_type="apartment",
        purpose="sale",
        city=city,
        neighborhood="Pinheiros",
        bedrooms=2,
        bathrooms=1,
        area_m2=65,
        active=True,
    )
    db.add(listing)
    db.flush()
    return listing


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    db_session.query(RealEstateListing).filter(
        RealEstateListing.url.like("https://example.com/imovel/%")
    ).delete(synchronize_session=False)
    db_session.query(RealEstateSource).filter(
        RealEstateSource.name.like("pytest-source-%")
    ).delete(synchronize_session=False)
    db_session.commit()


# ── GET /api/v1/real-estate/sources ──────────────────────────────────────────

def test_list_sources_returns_200(api_client, db_session):
    _make_source(db_session)
    db_session.commit()

    r = api_client.get("/api/v1/real-estate/sources")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_sources_contains_created_source(api_client, db_session):
    source = _make_source(db_session, city="Curitiba")
    db_session.commit()

    names = [s["name"] for s in api_client.get("/api/v1/real-estate/sources").json()]
    assert source.name in names


# ── GET /api/v1/real-estate/listings ─────────────────────────────────────────

def test_list_listings_returns_200(api_client, db_session):
    source = _make_source(db_session)
    _make_listing(db_session, source)
    db_session.commit()

    r = api_client.get("/api/v1/real-estate/listings")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_listings_respects_limit(api_client, db_session):
    source = _make_source(db_session)
    for _ in range(5):
        _make_listing(db_session, source)
    db_session.commit()

    r = api_client.get("/api/v1/real-estate/listings?limit=2")
    assert r.status_code == 200
    assert len(r.json()) <= 2


def test_list_listings_filters_by_city(api_client, db_session):
    source = _make_source(db_session, city="Florianópolis")
    listing = _make_listing(db_session, source, city="Florianópolis")
    db_session.commit()

    data = api_client.get("/api/v1/real-estate/listings?city=Florianópolis").json()
    ids = [item["id"] for item in data]
    assert listing.id in ids


# ── GET /api/v1/real-estate/listings/{id} ────────────────────────────────────

def test_get_listing_returns_correct_item(api_client, db_session):
    source = _make_source(db_session)
    listing = _make_listing(db_session, source)
    db_session.commit()

    r = api_client.get(f"/api/v1/real-estate/listings/{listing.id}")
    assert r.status_code == 200
    assert r.json()["id"] == listing.id
    assert r.json()["title"] == listing.title


def test_get_listing_returns_404_for_unknown_id(api_client):
    r = api_client.get(f"/api/v1/real-estate/listings/{uuid4()}")
    assert r.status_code == 404


# ── GET /api/v1/real-estate/listings/{id}/price-history ──────────────────────

def test_price_history_returns_empty_list_when_no_history(api_client, db_session):
    source = _make_source(db_session)
    listing = _make_listing(db_session, source)
    db_session.commit()

    r = api_client.get(f"/api/v1/real-estate/listings/{listing.id}/price-history")
    assert r.status_code == 200
    assert r.json() == []


def test_price_history_respects_limit(api_client, db_session):
    from app.modules.real_estate.models import RealEstatePriceHistory
    from datetime import datetime, timezone

    source = _make_source(db_session)
    listing = _make_listing(db_session, source)
    for i in range(5):
        db_session.add(RealEstatePriceHistory(
            id=str(uuid4()),
            listing_id=listing.id,
            price=500_000.0 + i * 1_000,
            collected_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    r = api_client.get(f"/api/v1/real-estate/listings/{listing.id}/price-history?limit=3")
    assert r.status_code == 200
    assert len(r.json()) <= 3

    db_session.query(RealEstatePriceHistory).filter(
        RealEstatePriceHistory.listing_id == listing.id
    ).delete(synchronize_session=False)
    db_session.commit()

"""
test_sports_odds_api.py

Integration tests for GET /api/v1/sports-odds/* endpoints.
Seeds minimal records via SQLAlchemy directly (no service layer), then
asserts HTTP responses via the FastAPI TestClient.
"""

from uuid import uuid4
from datetime import datetime, timezone

UTC = timezone.utc

import pytest
from sqlalchemy.orm import Session

from app.modules.sports_odds.models import (
    SportsBook,
    SportsLeague,
    SportsEvent,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_sportsbook(db: Session) -> SportsBook:
    sb = SportsBook(
        id=str(uuid4()),
        name=f"pytest-book-{uuid4().hex[:6]}",
        base_url="https://bookmaker.example.com",
        active=True,
    )
    db.add(sb)
    db.flush()
    return sb


def _make_league(db: Session, *, sport: str = "football", league_name: str | None = None) -> SportsLeague:
    league = SportsLeague(
        id=str(uuid4()),
        sport=sport,
        league_name=league_name or f"pytest-league-{uuid4().hex[:6]}",
        country="Brazil",
        active=True,
    )
    db.add(league)
    db.flush()
    return league


def _make_event(db: Session, league: SportsLeague, *, status: str = "scheduled") -> SportsEvent:
    event = SportsEvent(
        id=str(uuid4()),
        league_id=league.id,
        home_team="Flamengo",
        away_team="Palmeiras",
        start_time=datetime(2026, 12, 1, 20, 0, tzinfo=UTC),
        event_status=status,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(event)
    db.flush()
    return event


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    db_session.query(SportsEvent).filter(
        SportsEvent.home_team == "Flamengo"
    ).delete(synchronize_session=False)
    db_session.query(SportsLeague).filter(
        SportsLeague.league_name.like("pytest-league-%")
    ).delete(synchronize_session=False)
    db_session.query(SportsBook).filter(
        SportsBook.name.like("pytest-book-%")
    ).delete(synchronize_session=False)
    db_session.commit()


# ── GET /api/v1/sports-odds/sportsbooks ──────────────────────────────────────

def test_list_sportsbooks_returns_200(api_client, db_session):
    _make_sportsbook(db_session)
    db_session.commit()

    r = api_client.get("/api/v1/sports-odds/sportsbooks")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_sportsbooks_contains_created_book(api_client, db_session):
    sb = _make_sportsbook(db_session)
    db_session.commit()

    names = [b["name"] for b in api_client.get("/api/v1/sports-odds/sportsbooks").json()]
    assert sb.name in names


# ── GET /api/v1/sports-odds/leagues ──────────────────────────────────────────

def test_list_leagues_returns_200(api_client, db_session):
    _make_league(db_session)
    db_session.commit()

    r = api_client.get("/api/v1/sports-odds/leagues")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_leagues_contains_created_league(api_client, db_session):
    league = _make_league(db_session, sport="basketball")
    db_session.commit()

    sports = [lg["sport"] for lg in api_client.get("/api/v1/sports-odds/leagues").json()]
    assert "basketball" in sports


# ── GET /api/v1/sports-odds/events ───────────────────────────────────────────

def test_list_events_returns_200(api_client, db_session):
    league = _make_league(db_session)
    _make_event(db_session, league)
    db_session.commit()

    r = api_client.get("/api/v1/sports-odds/events")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_events_respects_limit(api_client, db_session):
    league = _make_league(db_session)
    for _ in range(5):
        _make_event(db_session, league)
    db_session.commit()

    r = api_client.get("/api/v1/sports-odds/events?limit=2")
    assert r.status_code == 200
    assert len(r.json()) <= 2


def test_list_events_filters_by_status(api_client, db_session):
    league = _make_league(db_session)
    ev = _make_event(db_session, league, status="live")
    db_session.commit()

    data = api_client.get("/api/v1/sports-odds/events?status=live").json()
    ids = [e["id"] for e in data]
    assert ev.id in ids


def test_list_events_filters_by_league_name(api_client, db_session):
    unique_name = f"pytest-league-{uuid4().hex[:8]}"
    league = _make_league(db_session, league_name=unique_name)
    ev = _make_event(db_session, league)
    db_session.commit()

    data = api_client.get(f"/api/v1/sports-odds/events?league_name={unique_name}").json()
    ids = [e["id"] for e in data]
    assert ev.id in ids


# ── GET /api/v1/sports-odds/events/{id} ──────────────────────────────────────

def test_get_event_returns_correct_item(api_client, db_session):
    league = _make_league(db_session)
    ev = _make_event(db_session, league)
    db_session.commit()

    r = api_client.get(f"/api/v1/sports-odds/events/{ev.id}")
    assert r.status_code == 200
    assert r.json()["id"] == ev.id
    assert r.json()["home_team"] == "Flamengo"


def test_get_event_returns_404_for_unknown_id(api_client):
    r = api_client.get(f"/api/v1/sports-odds/events/{uuid4()}")
    assert r.status_code == 404


# ── GET /api/v1/sports-odds/events/{id}/odds-history ─────────────────────────

def test_odds_history_returns_empty_list_when_no_snapshots(api_client, db_session):
    league = _make_league(db_session)
    ev = _make_event(db_session, league)
    db_session.commit()

    r = api_client.get(f"/api/v1/sports-odds/events/{ev.id}/odds-history")
    assert r.status_code == 200
    assert r.json() == []

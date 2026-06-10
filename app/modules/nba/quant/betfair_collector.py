"""
NBA Betfair read-only connector.

Authentication: non-interactive (username + password + app key via env).
Requires betfairlightweight>=3.19.0 in requirements.txt.

Environment variables:
  BETFAIR_USERNAME    : Betfair account username
  BETFAIR_PASSWORD    : Betfair account password
  BETFAIR_APP_KEY     : application key from Betfair Developer portal
  BETFAIR_LOCALE      : "GB" (default) or "AU" — affects cert path selection

IMPORTANT: This module is read-only. It never calls placeOrders, updateOrders,
or any mutation endpoint. All operations are listEvents / listMarketCatalogue /
listMarketBook only.

Betfair VPS note: Betfair does NOT block datacenter IPs for read operations,
but some markets (UK exchange) may require specific account settings. If you
get ACCESS_DENIED, check the Betfair account's API access settings.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_USERNAME = os.environ.get("BETFAIR_USERNAME", "")
_PASSWORD = os.environ.get("BETFAIR_PASSWORD", "")
_APP_KEY = os.environ.get("BETFAIR_APP_KEY", "")

# Betfair event type ID for basketball
_BASKETBALL_EVENT_TYPE_ID = "7522"
# NBA competition IDs (US-based markets)
_NBA_COMPETITION_IDS = ["10972"  # NBA — Betfair exchange
]


@dataclass
class BetfairMarket:
    market_id: str
    market_name: str
    event_name: str
    event_id: str
    total_matched: float
    runners: list[dict] = field(default_factory=list)


@dataclass
class BetfairOdds:
    market_id: str
    market_name: str
    runners: list[dict]  # [{runner_name, best_back, best_lay}]


@dataclass
class BetfairCheckResult:
    connected: bool = False
    account_funds: float | None = None
    error: str | None = None
    markets_found: int = 0


def _get_client():
    """Create and login a betfairlightweight client. Returns client or raises."""
    try:
        import betfairlightweight
    except ImportError as exc:
        raise ImportError(
            "betfairlightweight not installed. Add 'betfairlightweight>=3.19.0' to requirements.txt"
        ) from exc

    if not _USERNAME or not _PASSWORD or not _APP_KEY:
        raise ValueError(
            "BETFAIR_USERNAME, BETFAIR_PASSWORD and BETFAIR_APP_KEY must all be set."
        )

    client = betfairlightweight.APIClient(
        username=_USERNAME,
        password=_PASSWORD,
        app_key=_APP_KEY,
    )
    client.login()
    return client


def check_connection() -> BetfairCheckResult:
    """
    Validate Betfair credentials and connectivity.
    Lists available funds to confirm auth works.
    Returns BetfairCheckResult (never raises).
    """
    result = BetfairCheckResult()
    try:
        client = _get_client()
        account = client.account.get_account_funds()
        result.connected = True
        result.account_funds = float(account.available_to_bet_balance)
        logger.info(
            "Betfair connection OK",
            extra={"funds": result.account_funds},
        )
    except Exception as exc:
        result.error = str(exc)
        logger.error("Betfair connection check failed: %s", exc)
    return result


def list_nba_events(days_ahead: int = 7) -> list[dict]:
    """
    List upcoming NBA events on Betfair exchange.
    Returns list of {event_id, event_name, open_date, country_code}.
    """
    from datetime import datetime, timedelta, timezone

    client = _get_client()

    from_dt = datetime.now(timezone.utc).isoformat()
    to_dt = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat()

    try:
        events = client.betting.list_events(
            filter=client.trading.MarketFilter(
                event_type_ids=[_BASKETBALL_EVENT_TYPE_ID],
                competition_ids=_NBA_COMPETITION_IDS,
                market_start_time={"from": from_dt, "to": to_dt},
            )
        )
    except AttributeError:
        # betfairlightweight >= 3.x uses different filter path
        import betfairlightweight.filters as bf_filters
        events = client.betting.list_events(
            filter=bf_filters.market_filter(
                event_type_ids=[_BASKETBALL_EVENT_TYPE_ID],
                competition_ids=_NBA_COMPETITION_IDS,
                market_start_time=bf_filters.time_range(from_=from_dt, to=to_dt),
            )
        )

    output = []
    for ev in events:
        e = ev.event
        output.append({
            "event_id": e.id,
            "event_name": e.name,
            "open_date": str(e.open_date) if e.open_date else None,
            "country_code": getattr(e, "country_code", None),
        })

    logger.info("Betfair NBA events listed", extra={"count": len(output)})
    return output


def list_markets(event_id: str) -> list[BetfairMarket]:
    """
    List available markets for a Betfair NBA event.
    Filters to: Match Odds (moneyline), Handicap (spread), Total Points (totals).
    """
    client = _get_client()
    _NBA_MARKET_TYPES = ["MATCH_ODDS", "ASIAN_HANDICAP", "TOTAL_GOALS"]

    try:
        import betfairlightweight.filters as bf_filters
        catalogues = client.betting.list_market_catalogue(
            filter=bf_filters.market_filter(
                event_ids=[event_id],
                market_types=_NBA_MARKET_TYPES,
            ),
            market_projection=["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            max_results=20,
        )
    except Exception:
        catalogues = client.betting.list_market_catalogue(
            filter=client.trading.MarketFilter(
                event_ids=[event_id],
                market_types=_NBA_MARKET_TYPES,
            ),
            market_projection=["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            max_results=20,
        )

    markets = []
    for cat in catalogues:
        runners = []
        for r in (cat.runners or []):
            runners.append({
                "runner_id": r.selection_id,
                "runner_name": r.runner_name,
                "handicap": r.handicap,
            })
        markets.append(BetfairMarket(
            market_id=cat.market_id,
            market_name=cat.market_name,
            event_name=cat.event.name if cat.event else "",
            event_id=event_id,
            total_matched=float(cat.total_matched or 0),
            runners=runners,
        ))

    logger.info("Betfair markets listed", extra={"event_id": event_id, "count": len(markets)})
    return markets


def get_odds(market_id: str) -> BetfairOdds | None:
    """
    Fetch best available back/lay odds for a market.
    Returns best back price for each runner (pre-game odds equivalent).
    Read-only: only listMarketBook is called.
    """
    client = _get_client()

    try:
        import betfairlightweight.filters as bf_filters
        books = client.betting.list_market_book(
            market_ids=[market_id],
            price_projection=bf_filters.price_projection(
                price_data=["EX_BEST_OFFERS"],
                ex_best_offers_overrides=bf_filters.ex_best_offers_overrides(
                    best_prices_depth=1,
                    rollup_model="STAKE",
                    rollup_limit=2,
                ),
            ),
        )
    except Exception:
        books = client.betting.list_market_book(
            market_ids=[market_id],
            price_projection={"priceData": ["EX_BEST_OFFERS"]},
        )

    if not books:
        return None

    book = books[0]
    runners = []
    for runner in (book.runners or []):
        best_back = None
        best_lay = None
        ex = getattr(runner, "ex", None)
        if ex:
            if ex.available_to_back:
                best_back = float(ex.available_to_back[0].price)
            if ex.available_to_lay:
                best_lay = float(ex.available_to_lay[0].price)
        runners.append({
            "runner_id": runner.selection_id,
            "status": runner.status,
            "best_back": best_back,
            "best_lay": best_lay,
        })

    return BetfairOdds(
        market_id=market_id,
        market_name="",
        runners=runners,
    )


def is_configured() -> bool:
    return bool(_USERNAME and _PASSWORD and _APP_KEY)


def validate_config() -> dict:
    return {
        "configured": is_configured(),
        "username_set": bool(_USERNAME),
        "password_set": bool(_PASSWORD),
        "app_key_set": bool(_APP_KEY),
        "note": "read-only — placeOrders is never called",
    }

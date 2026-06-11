from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import db_session
from app.modules.nba.quant.analytics import (
    GlobalAnalytics,
    SetupAnalytics,
    global_analytics,
    refresh_edge_registry,
)
from app.modules.nba.quant.models import (
    BetStatus,
    GameStatus,
    NbaEdgeRegistry,
    NbaGame,
    NbaQuantBet,
    NbaSignal,
)

router = APIRouter(prefix="/api/v1/nba/quant", tags=["nba-quant"])

_SETUP_NAMES = [
    "HOME_DOG_V1",
    "REST_ADVANTAGE_V1",
    "BACK_TO_BACK_FADE_V1",
    "PACE_OVER_V1",
]


# ── Response Schemas ───────────────────────────────────────────────────────────

class GameResponse(BaseModel):
    id: UUID
    external_id: str | None
    season: int
    game_date: datetime
    home_team: str
    away_team: str
    home_score: int | None
    away_score: int | None
    status: str
    model_config = ConfigDict(from_attributes=True)


class SignalResponse(BaseModel):
    id: UUID
    game_id: UUID
    setup_name: str
    market_type: str
    selection: str
    line: float | None
    odd: float
    signal_direction: str
    rationale: str | None
    confidence: float
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class QuantBetResponse(BaseModel):
    id: UUID
    signal_id: UUID
    stake: float
    status: str
    settled_at: datetime | None
    pnl: float | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class SetupAnalyticsResponse(BaseModel):
    setup_name: str
    total_bets: int
    wins: int
    losses: int
    pending: int
    void: int
    roi: float
    yield_pct: float
    win_rate: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    classification: str


class GlobalAnalyticsResponse(BaseModel):
    total_signals: int
    total_bets: int
    wins: int
    losses: int
    pending: int
    void: int
    roi: float
    pnl: float
    win_rate: float
    setups: list[SetupAnalyticsResponse]


class EdgeRegistryResponse(BaseModel):
    id: UUID
    setup_name: str
    total_bets: int
    wins: int
    losses: int
    roi: float
    yield_pct: float
    win_rate: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    classification: str
    last_updated: datetime
    model_config = ConfigDict(from_attributes=True)


class IngestGameRequest(BaseModel):
    external_id: str | None = None
    season: int
    game_date: datetime
    home_team: str
    away_team: str
    home_score: int | None = None
    away_score: int | None = None
    status: GameStatus = GameStatus.scheduled


class IngestOddsRequest(BaseModel):
    game_id: UUID
    bookmaker: str = "market"
    market_type: str
    selection: str
    line: float | None = None
    odd: float


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def quant_status(db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    total_games = db.query(NbaGame).count()
    final_games = db.query(NbaGame).filter(NbaGame.status == GameStatus.final).count()
    total_signals = db.query(NbaSignal).count()
    pending_bets = db.query(NbaQuantBet).filter(NbaQuantBet.status == BetStatus.pending).count()

    from app.modules.nba.quant.metrics import nba_q_total_games, nba_q_total_signals
    nba_q_total_games.set(total_games)
    nba_q_total_signals.set(total_signals)

    return {
        "status": "ok",
        "total_games": total_games,
        "final_games": final_games,
        "total_signals": total_signals,
        "pending_bets": pending_bets,
        "setups": _SETUP_NAMES,
    }


@router.get("/signals", response_model=list[SignalResponse])
def list_signals(
    setup: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(db_session),  # noqa: B008
) -> list[NbaSignal]:
    q = db.query(NbaSignal).order_by(NbaSignal.created_at.desc())
    if setup:
        q = q.filter(NbaSignal.setup_name == setup)
    return q.offset(offset).limit(limit).all()


@router.get("/paper-bets", response_model=list[QuantBetResponse])
def list_bets(
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(db_session),  # noqa: B008
) -> list[NbaQuantBet]:
    q = db.query(NbaQuantBet).order_by(NbaQuantBet.created_at.desc())
    if status:
        q = q.filter(NbaQuantBet.status == status)
    return q.offset(offset).limit(limit).all()


@router.get("/analytics", response_model=GlobalAnalyticsResponse)
def analytics(db: Session = Depends(db_session)) -> GlobalAnalytics:  # noqa: B008
    stats = global_analytics(db)
    _update_global_metrics(stats)
    return stats


@router.get("/analytics/{setup_name}", response_model=SetupAnalyticsResponse)
def setup_stats(setup_name: str, db: Session = Depends(db_session)) -> SetupAnalytics:  # noqa: B008
    from app.modules.nba.quant.analytics import setup_analytics
    if setup_name not in _SETUP_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown setup: {setup_name}")
    return setup_analytics(db, setup_name)


@router.get("/edge-registry", response_model=list[EdgeRegistryResponse])
def edge_registry(
    classification: str | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> list[NbaEdgeRegistry]:
    q = db.query(NbaEdgeRegistry).order_by(NbaEdgeRegistry.roi.desc())
    if classification:
        q = q.filter(NbaEdgeRegistry.classification == classification)
    return q.all()


@router.post("/edge-registry/refresh")
def refresh_registry(db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    records = refresh_edge_registry(db)
    return {
        "refreshed": len(records),
        "setups": [r.setup_name for r in records],
    }


@router.post("/games")
def ingest_game(req: IngestGameRequest, db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    from app.modules.nba.quant.models import NbaGame as G
    existing = db.query(G).filter(
        G.home_team == req.home_team,
        G.away_team == req.away_team,
        G.game_date == req.game_date,
    ).first()
    if existing:
        return {"id": str(existing.id), "action": "existing"}
    game = NbaGame(
        external_id=req.external_id,
        season=req.season,
        game_date=req.game_date,
        home_team=req.home_team,
        away_team=req.away_team,
        home_score=req.home_score,
        away_score=req.away_score,
        status=req.status,
    )
    db.add(game)
    db.commit()
    return {"id": str(game.id), "action": "created"}


@router.post("/odds")
def ingest_odds(req: IngestOddsRequest, db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    from app.modules.nba.quant.models import MarketType, NbaOdds
    game = db.query(NbaGame).filter(NbaGame.id == req.game_id).first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        mt = MarketType(req.market_type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid market_type: {req.market_type}")  # noqa: B904
    odds = NbaOdds(
        game_id=req.game_id,
        bookmaker=req.bookmaker,
        market_type=mt,
        selection=req.selection,
        line=req.line,
        odd=req.odd,
    )
    db.add(odds)
    db.commit()
    return {"id": str(odds.id), "action": "created"}


@router.post("/features/compute")
def compute_features_endpoint(
    game_id: UUID | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> dict[str, Any]:
    from app.modules.nba.quant.features import compute_all_pending, compute_features
    if game_id:
        feat = compute_features(db, game_id)
        return {"computed": 1 if feat else 0}
    count = compute_all_pending(db)
    return {"computed": count}


@router.post("/signals/generate")
def generate_signals_endpoint(
    game_id: UUID | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> dict[str, Any]:
    from app.modules.nba.quant.signals import generate_signals, run_all_games
    from app.modules.nba.quant.telegram_alerts import send_signal_alert

    if game_id:
        game = db.query(NbaGame).filter(NbaGame.id == game_id).first()
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
        signals = generate_signals(db, game)
        alerts = sum(
            1 for sig in signals
            if send_signal_alert(sig, game, getattr(game, "features", None), db=db)
        )
        return {"generated": len(signals), "alerts_sent": alerts}

    count = run_all_games(db)
    return {"generated": count, "alerts_sent": 0}


@router.post("/bets/settle")
def settle_bets(
    game_id: UUID | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> dict[str, Any]:
    from app.modules.nba.quant.paper_betting import settle_all_pending, settle_game
    if game_id:
        settled = settle_game(db, str(game_id))
        return {"settled": settled}
    total = settle_all_pending(db)
    return {"settled": total}


@router.post("/pipeline/run")
def run_pipeline(
    backfill: bool = False,
    seasons: str | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> dict[str, Any]:
    """
    Trigger the full NBA quant pipeline.

    - backfill=false (default): daily update only (recent games, no historical fetch)
    - backfill=true: full historical ingest + pipeline
    - seasons: comma-separated list of seasons to backfill (e.g. "2022,2023,2024")
    """
    from app.modules.nba.quant.pipeline import run_backfill, run_daily_update

    if backfill:
        season_list = [int(s.strip()) for s in seasons.split(",")] if seasons else None
        result = run_backfill(db, seasons=season_list)
    else:
        result = run_daily_update(db)

    return {
        "status": "ok" if result.ok else "partial_error",
        "duration_seconds": round(result.duration_seconds, 2),
        "seasons_fetched": result.seasons_fetched,
        "games_ingested": result.games_ingested,
        "recent_updated": result.recent_updated,
        "odds_upserted": result.odds_upserted,
        "odds_blocked": result.odds_blocked,
        "features_computed": result.features_computed,
        "signals_generated": result.signals_generated,
        "alerts_sent": result.alerts_sent,
        "bets_settled": result.bets_settled,
        "edge_registry_refreshed": result.edge_registry_refreshed,
        "errors": result.errors,
    }


# ── Phase 3: OOS validation ───────────────────────────────────────────────────

class WindowMetricsResponse(BaseModel):
    seasons: list[int]
    sample_size: int
    wins: int
    losses: int
    win_rate: float
    roi: float
    profit_factor: float
    max_drawdown: float
    sharpe: float
    total_pnl: float


class SetupOOSResponse(BaseModel):
    setup_name: str
    verdict: str
    notes: list[str]
    train: WindowMetricsResponse
    test: WindowMetricsResponse


@router.get("/oos/validate", response_model=list[SetupOOSResponse])
def oos_validate(
    train_seasons: str = "2022,2023",
    test_seasons: str = "2024",
    db: Session = Depends(db_session),  # noqa: B008
) -> list[dict]:
    """
    Run out-of-sample validation.

    Default split: train=2022,2023 / test=2024.
    Returns per-setup metrics and EDGE_CONFIRMED / EDGE_MARGINAL / EDGE_DEGRADED / NO_EDGE verdict.
    """
    from app.modules.nba.quant.out_of_sample import run_oos_validation

    train = [int(s.strip()) for s in train_seasons.split(",")]
    test = [int(s.strip()) for s in test_seasons.split(",")]
    results = run_oos_validation(db, train_seasons=train, test_seasons=test)

    def _wm(m: object) -> dict:
        return {
            "seasons": m.seasons,
            "sample_size": m.sample_size,
            "wins": m.wins,
            "losses": m.losses,
            "win_rate": round(m.win_rate, 4),
            "roi": round(m.roi, 4),
            "profit_factor": (
                round(m.profit_factor, 4) if m.profit_factor != float("inf") else 9999.0
            ),
            "max_drawdown": round(m.max_drawdown, 2),
            "sharpe": round(m.sharpe, 4),
            "total_pnl": round(m.total_pnl, 2),
        }

    output = [
        {
            "setup_name": r.setup_name,
            "verdict": r.verdict,
            "notes": r.notes,
            "train": _wm(r.train),
            "test": _wm(r.test),
        }
        for r in results
    ]
    _update_oos_metrics(output)
    return output


# ── Phase 3: Odds ingest ──────────────────────────────────────────────────────

@router.post("/odds/ingest")
def ingest_upcoming_odds(db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    """
    Fetch upcoming NBA odds from The Odds API and populate nba_odds.
    Requires THE_ODDS_API_KEY env var (free tier: upcoming games only).
    """
    from app.modules.nba.quant.odds_collector import fetch_upcoming_odds as _fetch

    result = _fetch(db)
    return {
        "status": "blocked" if result.blocked else ("ok" if result.ok else "error"),
        "blocked_reason": result.blocked_reason or None,
        "games_matched": result.games_matched,
        "odds_upserted": result.odds_upserted,
        "games_unmatched": len(result.games_unmatched),
        "requests_remaining": result.requests_remaining,
        "errors": result.errors,
    }


@router.post("/odds/backfill")
def backfill_historical_odds(
    seasons: str = "2022,2023,2024",
    db: Session = Depends(db_session),  # noqa: B008
) -> dict[str, Any]:
    """
    Backfill historical odds for given seasons from The Odds API.
    Requires paid plan with history access.
    """
    from app.modules.nba.quant.odds_collector import backfill_odds_for_seasons

    season_list = [int(s.strip()) for s in seasons.split(",")]
    result = backfill_odds_for_seasons(db, season_list)
    return {
        "status": "blocked" if result.blocked else ("ok" if result.ok else "error"),
        "blocked_reason": result.blocked_reason or None,
        "games_matched": result.games_matched,
        "odds_upserted": result.odds_upserted,
        "errors": result.errors[:10],
    }


# ── Phase 3: Telegram config ──────────────────────────────────────────────────

@router.get("/telegram/config")
def telegram_config() -> dict[str, Any]:
    """Check Telegram alert configuration status."""
    from app.modules.nba.quant.telegram_alerts import validate_config
    return validate_config()


@router.post("/telegram/test")
def telegram_test() -> dict[str, Any]:
    """Send a test Telegram alert to validate connectivity."""
    from app.modules.nba.quant.telegram_alerts import send_alert
    ok = send_alert(
        "🏀 *NBA Quant — Test Alert*\n\n"
        "Simulações NBA ativas via canal #executive.\n\n"
        "_Mensagem de teste._"
    )
    return {"sent": ok, "status": "ok" if ok else "failed"}


# ── Betfair endpoints ─────────────────────────────────────────────────────────

@router.get("/betfair/config")
def betfair_config() -> dict[str, Any]:
    """Check Betfair credentials configuration (never exposes secrets)."""
    from app.modules.nba.quant.betfair_collector import validate_config
    return validate_config()


@router.get("/betfair/status")
def betfair_status() -> dict[str, Any]:
    """Test Betfair connection: login + account funds check (read-only)."""
    from app.modules.nba.quant.betfair_collector import check_connection, is_configured
    if not is_configured():
        return {"connected": False, "error": "BETFAIR_USERNAME/PASSWORD/APP_KEY not set"}
    result = check_connection()
    return {
        "connected": result.connected,
        "account_funds": result.account_funds,
        "error": result.error,
    }


@router.get("/betfair/events")
def betfair_events(days_ahead: int = 7) -> dict[str, Any]:
    """List upcoming NBA events on Betfair (read-only)."""
    from app.modules.nba.quant.betfair_collector import is_configured, list_nba_events
    if not is_configured():
        return {"events": [], "error": "Betfair not configured"}
    try:
        events = list_nba_events(days_ahead=days_ahead)
        return {"events": events, "count": len(events)}
    except Exception as exc:
        return {"events": [], "error": str(exc)}


@router.get("/betfair/markets/{event_id}")
def betfair_markets(event_id: str) -> dict[str, Any]:
    """List markets for a Betfair NBA event (read-only)."""
    from app.modules.nba.quant.betfair_collector import is_configured, list_markets
    if not is_configured():
        return {"markets": [], "error": "Betfair not configured"}
    try:
        markets = list_markets(event_id)
        return {
            "markets": [
                {
                    "market_id": m.market_id,
                    "market_name": m.market_name,
                    "total_matched": m.total_matched,
                    "runners": m.runners,
                }
                for m in markets
            ],
            "count": len(markets),
        }
    except Exception as exc:
        return {"markets": [], "error": str(exc)}


@router.get("/betfair/odds/{market_id}")
def betfair_odds(market_id: str) -> dict[str, Any]:
    """Fetch best back/lay odds for a Betfair market (read-only)."""
    from app.modules.nba.quant.betfair_collector import get_odds, is_configured
    if not is_configured():
        return {"runners": [], "error": "Betfair not configured"}
    try:
        result = get_odds(market_id)
        if result is None:
            return {"runners": [], "error": "No market book found"}
        return {"market_id": result.market_id, "runners": result.runners}
    except Exception as exc:
        return {"runners": [], "error": str(exc)}


# ── Metrics helpers ────────────────────────────────────────────────────────────

def _update_global_metrics(stats: GlobalAnalytics) -> None:
    from app.modules.nba.quant.metrics import (
        nba_q_global_pnl,
        nba_q_global_roi,
        nba_q_setup_classification,
        nba_q_setup_roi,
        nba_q_setup_win_rate,
    )
    nba_q_global_roi.set(stats.roi)
    nba_q_global_pnl.set(stats.pnl)
    for s in stats.setups:
        nba_q_setup_roi.labels(setup=s.setup_name).set(s.roi)
        nba_q_setup_win_rate.labels(setup=s.setup_name).set(s.win_rate)
        cls_val = {"profitable": 1, "neutral": 0, "losing": -1}.get(s.classification, 0)
        nba_q_setup_classification.labels(setup=s.setup_name).set(cls_val)


def _update_oos_metrics(results: list) -> None:
    from app.modules.nba.quant.metrics import (
        nba_q_oos_roi,
        nba_q_oos_verdict,
        nba_q_setup_max_drawdown,
        nba_q_setup_profit_factor,
        nba_q_setup_sharpe,
    )
    verdict_map = {"EDGE_CONFIRMED": 2, "EDGE_MARGINAL": 1, "EDGE_DEGRADED": 0, "NO_EDGE": -1}
    for r in results:
        s = r["setup_name"]
        nba_q_oos_verdict.labels(setup=s).set(verdict_map.get(r["verdict"], -1))
        for window in ("train", "test"):
            m = r[window]
            nba_q_oos_roi.labels(setup=s, window=window).set(m["roi"])
            nba_q_setup_sharpe.labels(setup=s, window=window).set(m["sharpe"])
            nba_q_setup_profit_factor.labels(setup=s, window=window).set(
                min(m["profit_factor"], 9999.0)
            )
            nba_q_setup_max_drawdown.labels(setup=s, window=window).set(m["max_drawdown"])

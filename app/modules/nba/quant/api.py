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
    if game_id:
        game = db.query(NbaGame).filter(NbaGame.id == game_id).first()
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
        signals = generate_signals(db, game)
        return {"generated": len(signals)}
    count = run_all_games(db)
    return {"generated": count}


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
        cls_val = {"PROFITABLE": 1, "NEUTRAL": 0, "LOSING": -1}.get(s.classification, 0)
        nba_q_setup_classification.labels(setup=s.setup_name).set(cls_val)

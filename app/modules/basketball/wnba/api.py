"""WNBA Quant REST API — mirrors NBA quant API structure."""
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import db_session
from app.modules.basketball.shared.analytics_base import GlobalAnalytics, SetupAnalytics
from app.modules.basketball.shared.enums import BetStatus, GameStatus
from app.modules.basketball.wnba.analytics import global_analytics, refresh_edge_registry
from app.modules.basketball.wnba.models import (
    WnbaEdgeRegistry,
    WnbaGame,
    WnbaQuantBet,
    WnbaSignal,
)

router = APIRouter(prefix="/api/v1/wnba/quant", tags=["wnba-quant"])

_SETUP_NAMES = [
    "REST_ADVANTAGE_V1",
    "HOME_DOG_V1",
    "BACK_TO_BACK_FADE_V1",
    "TOTAL_PACE_V1",
    "SPREAD_VALUE_V1",
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


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def quant_status(db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    from app.modules.basketball.wnba.metrics import wnba_q_total_games, wnba_q_total_signals

    total_games = db.query(WnbaGame).count()
    final_games = db.query(WnbaGame).filter(WnbaGame.status == GameStatus.final).count()
    total_signals = db.query(WnbaSignal).count()
    pending_bets = db.query(WnbaQuantBet).filter(WnbaQuantBet.status == BetStatus.pending).count()

    wnba_q_total_games.set(total_games)
    wnba_q_total_signals.set(total_signals)

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
) -> list[WnbaSignal]:
    q = db.query(WnbaSignal).order_by(WnbaSignal.created_at.desc())
    if setup:
        q = q.filter(WnbaSignal.setup_name == setup)
    return q.offset(offset).limit(limit).all()


@router.get("/paper-bets", response_model=list[QuantBetResponse])
def list_bets(
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(db_session),  # noqa: B008
) -> list[WnbaQuantBet]:
    q = db.query(WnbaQuantBet).order_by(WnbaQuantBet.created_at.desc())
    if status:
        q = q.filter(WnbaQuantBet.status == status)
    return q.offset(offset).limit(limit).all()


@router.get("/analytics", response_model=GlobalAnalyticsResponse)
def analytics(db: Session = Depends(db_session)) -> GlobalAnalytics:  # noqa: B008
    stats = global_analytics(db)
    _update_global_metrics(stats)
    return stats


@router.get("/analytics/{setup_name}", response_model=SetupAnalyticsResponse)
def setup_stats(setup_name: str, db: Session = Depends(db_session)) -> SetupAnalytics:  # noqa: B008
    from app.modules.basketball.wnba.analytics import setup_analytics
    if setup_name not in _SETUP_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown setup: {setup_name}")
    return setup_analytics(db, setup_name)


@router.get("/edge-registry", response_model=list[EdgeRegistryResponse])
def edge_registry(
    classification: str | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> list[WnbaEdgeRegistry]:
    q = db.query(WnbaEdgeRegistry).order_by(WnbaEdgeRegistry.roi.desc())
    if classification:
        q = q.filter(WnbaEdgeRegistry.classification == classification)
    return q.all()


@router.post("/edge-registry/refresh")
def refresh_registry(db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    records = refresh_edge_registry(db)
    return {"refreshed": len(records), "setups": [r.setup_name for r in records]}


@router.post("/games")
def ingest_game(req: IngestGameRequest, db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    existing = db.query(WnbaGame).filter(
        WnbaGame.home_team == req.home_team,
        WnbaGame.away_team == req.away_team,
        WnbaGame.game_date == req.game_date,
    ).first()
    if existing:
        return {"id": str(existing.id), "action": "existing"}
    game = WnbaGame(
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


@router.post("/features/compute")
def compute_features_endpoint(
    game_id: UUID | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> dict[str, Any]:
    from app.modules.basketball.wnba.features import compute_all_pending, compute_features
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
    from app.modules.basketball.wnba.signals import generate_signals, run_all_games
    from app.modules.basketball.wnba.telegram_alerts import send_signal_alert

    if game_id:
        game = db.query(WnbaGame).filter(WnbaGame.id == game_id).first()
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
    from app.modules.basketball.wnba.paper_betting import settle_all_pending, settle_game
    if game_id:
        return {"settled": settle_game(db, str(game_id))}
    return {"settled": settle_all_pending(db)}


@router.post("/pipeline/run")
def run_pipeline(
    backfill: bool = False,
    seasons: str | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> dict[str, Any]:
    from app.modules.basketball.wnba.pipeline import run_backfill, run_daily_update

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


@router.get("/telegram/config")
def telegram_config() -> dict[str, Any]:
    from app.modules.basketball.wnba.telegram_alerts import validate_config
    return validate_config()


@router.post("/telegram/test")
def telegram_test() -> dict[str, Any]:
    from app.modules.basketball.wnba.telegram_alerts import send_alert
    ok = send_alert(
        "🏀 *WNBA Quant — Test Alert*\n\n"
        "Simulações WNBA ativas via canal #executive.\n\n"
        "_Mensagem de teste._"
    )
    return {"sent": ok, "status": "ok" if ok else "failed"}


@router.post("/odds/ingest")
def ingest_upcoming_odds(db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    from app.modules.basketball.wnba.odds_collector import fetch_upcoming_odds as _fetch
    result = _fetch(db)
    return {
        "status": "blocked" if result.blocked else ("ok" if result.ok else "error"),
        "blocked_reason": result.blocked_reason or None,
        "games_matched": result.games_matched,
        "odds_upserted": result.odds_upserted,
        "games_unmatched": len(result.games_unmatched),
        "errors": result.errors,
    }


# ── Metrics helpers ────────────────────────────────────────────────────────────

def _update_global_metrics(stats: GlobalAnalytics) -> None:
    from app.modules.basketball.wnba.metrics import (
        wnba_q_global_pnl,
        wnba_q_global_roi,
        wnba_q_setup_classification,
        wnba_q_setup_roi,
        wnba_q_setup_win_rate,
    )
    wnba_q_global_roi.set(stats.roi)
    wnba_q_global_pnl.set(stats.pnl)
    for s in stats.setups:
        wnba_q_setup_roi.labels(setup=s.setup_name).set(s.roi)
        wnba_q_setup_win_rate.labels(setup=s.setup_name).set(s.win_rate)
        cls_val = {"profitable": 1, "neutral": 0, "losing": -1}.get(s.classification, 0)
        wnba_q_setup_classification.labels(setup=s.setup_name).set(cls_val)

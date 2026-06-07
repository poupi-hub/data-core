from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import db_session
from app.modules.nba.analytics import GlobalStats, SourceStats, global_stats, source_stats
from app.modules.nba.capture import CaptureRequest, CaptureResult, capture_pick
from app.modules.nba.models import (
    NbaPaperBet,
    NbaPick,
    NbaSource,
    PickStatus,
    SourceType,
)
from app.modules.nba.paper_betting import settle_bet

router = APIRouter(prefix="/api/v1/nba", tags=["nba"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class SourceResponse(BaseModel):
    id: UUID
    name: str
    source_type: str
    handle: str | None
    active: bool
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class PickResponse(BaseModel):
    id: UUID
    source_id: UUID
    raw_text: str
    pick_type: str
    team: str | None
    player: str | None
    line: float | None
    odd: float
    event_description: str | None
    league: str
    event_time: datetime | None
    captured_at: datetime
    parse_status: str
    model_config = ConfigDict(from_attributes=True)


class PaperBetResponse(BaseModel):
    id: UUID
    pick_id: UUID
    stake: float
    status: str
    settled_at: datetime | None
    pnl: float | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class CapturePickRequest(BaseModel):
    source_name: str
    source_type: SourceType = SourceType.manual
    raw_text: str
    event_description: str | None = None
    event_time: datetime | None = None
    league: str = "NBA"


class SettleBetRequest(BaseModel):
    pick_id: str
    result: PickStatus


class SourceStatsResponse(BaseModel):
    source_id: str
    source_name: str
    total_picks: int
    wins: int
    losses: int
    pending: int
    void: int
    win_rate: float
    roi: float
    yield_pct: float
    pnl: float
    max_drawdown: float


class GlobalStatsResponse(BaseModel):
    total_picks: int
    total_settled: int
    wins: int
    losses: int
    pending: int
    void: int
    win_rate: float
    roi: float
    pnl: float
    sources: list[SourceStatsResponse]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def nba_status(db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    total_picks = db.query(NbaPick).count()
    total_sources = db.query(NbaSource).filter(NbaSource.active == True).count()  # noqa: E712
    pending_bets = db.query(NbaPaperBet).filter(NbaPaperBet.status == PickStatus.pending).count()
    return {
        "status": "ok",
        "total_picks": total_picks,
        "active_sources": total_sources,
        "pending_bets": pending_bets,
    }


@router.get("/picks", response_model=list[PickResponse])
def list_picks(
    limit: int = 50,
    offset: int = 0,
    source_id: UUID | None = None,
    db: Session = Depends(db_session),  # noqa: B008
) -> list[NbaPick]:
    q = db.query(NbaPick).order_by(NbaPick.captured_at.desc())
    if source_id:
        q = q.filter(NbaPick.source_id == source_id)
    return q.offset(offset).limit(limit).all()


@router.post("/picks", response_model=CaptureResult)
def ingest_pick(req: CapturePickRequest, db: Session = Depends(db_session)) -> CaptureResult:  # noqa: B008
    return capture_pick(
        db,
        CaptureRequest(
            source_name=req.source_name,
            source_type=req.source_type,
            raw_text=req.raw_text,
            event_description=req.event_description,
            event_time=req.event_time,
            league=req.league,
        ),
    )


@router.get("/paper-bets", response_model=list[PaperBetResponse])
def list_paper_bets(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(db_session),  # noqa: B008
) -> list[NbaPaperBet]:
    q = db.query(NbaPaperBet).order_by(NbaPaperBet.created_at.desc())
    if status:
        q = q.filter(NbaPaperBet.status == status)
    return q.offset(offset).limit(limit).all()


@router.post("/paper-bets/settle")
def settle(req: SettleBetRequest, db: Session = Depends(db_session)) -> dict[str, Any]:  # noqa: B008
    bet = settle_bet(db, req.pick_id, req.result)
    if not bet:
        raise HTTPException(status_code=404, detail="Bet not found or already settled")
    return {"id": str(bet.id), "status": bet.status, "pnl": float(bet.pnl or 0)}


@router.get("/analytics", response_model=GlobalStatsResponse)
def analytics(db: Session = Depends(db_session)) -> GlobalStats:  # noqa: B008
    stats = global_stats(db)
    _update_global_metrics(stats)
    return stats


@router.get("/sources", response_model=list[SourceResponse])
def list_sources(db: Session = Depends(db_session)) -> list[NbaSource]:  # noqa: B008
    return db.query(NbaSource).order_by(NbaSource.name).all()


@router.get("/sources/{source_id}/stats", response_model=SourceStatsResponse)
def get_source_stats(source_id: UUID, db: Session = Depends(db_session)) -> SourceStats:  # noqa: B008
    stats = source_stats(db, source_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Source not found")
    return stats


# ── Metrics refresh ───────────────────────────────────────────────────────────

def _update_global_metrics(stats: GlobalStats) -> None:
    from app.modules.nba.metrics import (
        nba_global_pnl,
        nba_global_roi,
        nba_global_win_rate,
        nba_source_pnl,
        nba_source_roi,
        nba_source_win_rate,
    )

    nba_global_roi.set(stats.roi)
    nba_global_pnl.set(stats.pnl)
    nba_global_win_rate.set(stats.win_rate)
    for s in stats.sources:
        nba_source_roi.labels(source=s.source_name).set(s.roi)
        nba_source_pnl.labels(source=s.source_name).set(s.pnl)
        nba_source_win_rate.labels(source=s.source_name).set(s.win_rate)

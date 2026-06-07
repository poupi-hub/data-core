"""
NBA analytics: ROI, win rate, yield, PnL, drawdown per source and overall.
"""
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.nba.models import NbaPaperBet, NbaPick, NbaSource, PickStatus


@dataclass
class SourceStats:
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


@dataclass
class GlobalStats:
    total_picks: int
    total_settled: int
    wins: int
    losses: int
    pending: int
    void: int
    win_rate: float
    roi: float
    pnl: float
    sources: list[SourceStats] = field(default_factory=list)


def _compute_drawdown(pnl_sequence: list[float]) -> float:
    if not pnl_sequence:
        return 0.0
    peak = 0.0
    cumulative = 0.0
    max_dd = 0.0
    for p in pnl_sequence:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def source_stats(db: Session, source_id: UUID) -> SourceStats | None:
    source = db.query(NbaSource).filter(NbaSource.id == source_id).first()
    if not source:
        return None

    bets = (
        db.query(NbaPaperBet)
        .join(NbaPick, NbaPaperBet.pick_id == NbaPick.id)
        .filter(NbaPick.source_id == source_id)
        .order_by(NbaPaperBet.created_at)
        .all()
    )

    wins = sum(1 for b in bets if b.status == PickStatus.won)
    losses = sum(1 for b in bets if b.status == PickStatus.lost)
    pending = sum(1 for b in bets if b.status == PickStatus.pending)
    void = sum(1 for b in bets if b.status == PickStatus.void)
    settled = wins + losses
    total_staked = settled * 1.0
    pnl = sum(float(b.pnl or 0) for b in bets if b.status in (PickStatus.won, PickStatus.lost))
    roi = round((pnl / total_staked) * 100, 2) if total_staked > 0 else 0.0
    win_rate = round(wins / settled * 100, 2) if settled > 0 else 0.0
    drawdown = _compute_drawdown([float(b.pnl or 0) for b in bets if b.status in (PickStatus.won, PickStatus.lost)])  # noqa: E501

    return SourceStats(
        source_id=str(source_id),
        source_name=source.name,
        total_picks=len(bets),
        wins=wins,
        losses=losses,
        pending=pending,
        void=void,
        win_rate=win_rate,
        roi=roi,
        yield_pct=roi,
        pnl=round(pnl, 4),
        max_drawdown=drawdown,
    )


def global_stats(db: Session) -> GlobalStats:
    bets = (
        db.query(NbaPaperBet)
        .join(NbaPick, NbaPaperBet.pick_id == NbaPick.id)
        .order_by(NbaPaperBet.created_at)
        .all()
    )

    wins = sum(1 for b in bets if b.status == PickStatus.won)
    losses = sum(1 for b in bets if b.status == PickStatus.lost)
    pending = sum(1 for b in bets if b.status == PickStatus.pending)
    void = sum(1 for b in bets if b.status == PickStatus.void)
    settled = wins + losses
    total_staked = settled * 1.0
    pnl = sum(float(b.pnl or 0) for b in bets if b.status in (PickStatus.won, PickStatus.lost))
    roi = round((pnl / total_staked) * 100, 2) if total_staked > 0 else 0.0
    win_rate = round(wins / settled * 100, 2) if settled > 0 else 0.0

    sources = db.query(NbaSource).filter(NbaSource.active == True).all()  # noqa: E712
    source_stats_list = [s for src in sources if (s := source_stats(db, src.id)) is not None]
    source_stats_list.sort(key=lambda s: s.roi, reverse=True)

    return GlobalStats(
        total_picks=len(bets),
        total_settled=settled,
        wins=wins,
        losses=losses,
        pending=pending,
        void=void,
        win_rate=win_rate,
        roi=roi,
        pnl=round(pnl, 4),
        sources=source_stats_list,
    )

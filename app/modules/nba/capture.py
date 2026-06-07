"""
NBA pick capture service.
Ingests raw text picks from sources (Telegram, Discord, X, manual).
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.modules.nba.models import NbaPaperBet, NbaPick, NbaSource, PickStatus, SourceType
from app.modules.nba.parser import parse_pick


@dataclass
class CaptureRequest:
    source_name: str
    source_type: SourceType
    raw_text: str
    event_description: str | None = None
    event_time: datetime | None = None
    league: str = "NBA"


@dataclass
class CaptureResult:
    pick_id: str
    source_id: str
    parse_status: str
    pick_type: str
    odd: float


def get_or_create_source(db: Session, name: str, source_type: SourceType, handle: str | None = None) -> NbaSource:  # noqa: E501
    source = db.query(NbaSource).filter(NbaSource.name == name).first()
    if source:
        return source
    source = NbaSource(name=name, source_type=source_type, handle=handle)
    db.add(source)
    db.flush()
    return source


def capture_pick(db: Session, req: CaptureRequest) -> CaptureResult:
    from app.modules.nba.metrics import nba_parse_errors_total, nba_picks_total

    source = get_or_create_source(db, req.source_name, req.source_type)
    parsed = parse_pick(req.raw_text, event_description=req.event_description)

    if parsed.parse_status == "error":
        nba_parse_errors_total.labels(source=req.source_name).inc()

    pick = NbaPick(
        source_id=source.id,
        raw_text=req.raw_text,
        pick_type=parsed.pick_type,
        team=parsed.team,
        player=parsed.player,
        line=float(parsed.line) if parsed.line is not None else None,
        odd=float(parsed.odd),
        event_description=req.event_description or parsed.event_description,
        league=req.league,
        event_time=req.event_time,
        parse_status=parsed.parse_status,
    )
    db.add(pick)
    db.flush()

    paper_bet = NbaPaperBet(
        pick_id=pick.id,
        stake=1.0,
        status=PickStatus.pending,
    )
    db.add(paper_bet)
    db.commit()

    nba_picks_total.labels(source=req.source_name, pick_type=parsed.pick_type.value).inc()

    return CaptureResult(
        pick_id=str(pick.id),
        source_id=str(source.id),
        parse_status=parsed.parse_status,
        pick_type=parsed.pick_type.value,
        odd=float(parsed.odd),
    )

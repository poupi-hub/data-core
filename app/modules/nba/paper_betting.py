"""
Paper betting service: settle bets against results, compute PnL.
American odds: stake 1 unit.
  - Won: pnl = (odd > 0) ? odd/100 : 100/abs(odd)
  - Lost: pnl = -1.0
  - Void: pnl = 0.0
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.modules.nba.models import NbaPaperBet, NbaPick, PickStatus


def _pnl_for_win(odd: float, stake: float = 1.0) -> float:
    if odd > 0:
        return round(stake * odd / 100, 4)
    return round(stake * 100 / abs(odd), 4)


def settle_bet(db: Session, pick_id: str, result: PickStatus) -> NbaPaperBet | None:
    from app.modules.nba.metrics import nba_bets_settled_total

    bet = db.query(NbaPaperBet).filter(NbaPaperBet.pick_id == pick_id).first()
    if not bet or bet.status != PickStatus.pending:
        return None

    pick = db.query(NbaPick).filter(NbaPick.id == pick_id).first()
    odd = float(pick.odd) if pick else -110.0

    if result == PickStatus.won:
        pnl = _pnl_for_win(odd, float(bet.stake))
    elif result == PickStatus.lost:
        pnl = -float(bet.stake)
    else:
        pnl = 0.0

    bet.status = result
    bet.settled_at = datetime.now(timezone.utc)
    bet.pnl = pnl
    db.commit()

    nba_bets_settled_total.labels(result=result.value).inc()
    return bet

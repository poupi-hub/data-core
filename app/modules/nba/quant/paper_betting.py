"""
Quant paper bet settlement.
American odds, 1-unit flat stake.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.modules.nba.quant.models import (
    BetStatus,
    GameStatus,
    MarketType,
    NbaGame,
    NbaQuantBet,
    NbaSignal,
    SignalDirection,
)


def _pnl(odd: float, stake: float, won: bool) -> float:
    if not won:
        return -stake
    if odd == 0:
        raise ValueError(f"Invalid odd value: {odd!r}")
    if odd >= 0:
        return round(stake * odd / 100, 4)
    return round(stake * 100 / abs(odd), 4)


def _determine_result(game: NbaGame, signal: NbaSignal) -> BetStatus | None:
    """Determine bet result from final game score. Returns None if can't determine."""
    if game.status != GameStatus.final:
        return None
    if game.home_score is None or game.away_score is None:
        return None

    hs, as_ = game.home_score, game.away_score
    total = hs + as_

    if signal.market_type == MarketType.moneyline:
        if signal.signal_direction == SignalDirection.home:
            return BetStatus.won if hs > as_ else BetStatus.lost
        return BetStatus.won if as_ > hs else BetStatus.lost

    if signal.market_type == MarketType.spread:
        if signal.line is None:
            return None
        line = float(signal.line)
        if signal.signal_direction == SignalDirection.home:
            margin = hs + line - as_
        else:
            margin = as_ + line - hs
        if abs(margin) < 0.01:
            return BetStatus.void
        return BetStatus.won if margin > 0 else BetStatus.lost

    if signal.market_type == MarketType.totals:
        if signal.line is None:
            return None
        line = float(signal.line)
        if abs(total - line) < 0.01:
            return BetStatus.void
        if signal.signal_direction == SignalDirection.over:
            return BetStatus.won if total > line else BetStatus.lost
        return BetStatus.won if total < line else BetStatus.lost

    return None


def settle_game(db: Session, game_id: str) -> int:
    """Settle all pending bets for a finished game. Returns number settled."""
    from app.modules.nba.quant.metrics import nba_q_bets_settled_total

    game = db.query(NbaGame).filter(NbaGame.id == game_id).first()
    if not game or game.status != GameStatus.final:
        return 0

    bets = (
        db.query(NbaQuantBet)
        .join(NbaSignal, NbaQuantBet.signal_id == NbaSignal.id)
        .filter(NbaSignal.game_id == game_id, NbaQuantBet.status == BetStatus.pending)
        .all()
    )

    settled = 0
    for bet in bets:
        result = _determine_result(game, bet.signal)
        if result is None:
            continue
        odd = float(bet.signal.odd)
        bet.status = result
        bet.settled_at = datetime.now(timezone.utc)
        bet.pnl = _pnl(odd, float(bet.stake), result == BetStatus.won)
        if result == BetStatus.void:
            bet.pnl = 0.0
        settled += 1
        nba_q_bets_settled_total.labels(
            setup=bet.signal.setup_name, result=result.value
        ).inc()

    db.commit()

    # Send settlement alerts (best-effort, never blocks settlement)
    try:
        from app.modules.nba.quant.telegram_alerts import send_settlement_alert
        for bet in bets:
            if bet.status != BetStatus.pending:
                send_settlement_alert(bet.signal, bet, game, db=db)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Settlement Telegram alert failed: %s", exc)

    return settled


def settle_all_pending(db: Session) -> int:
    """Settle all pending bets for finished games."""
    pending_game_ids = (
        db.query(NbaSignal.game_id)
        .join(NbaQuantBet, NbaSignal.id == NbaQuantBet.signal_id)
        .join(NbaGame, NbaSignal.game_id == NbaGame.id)
        .filter(NbaQuantBet.status == BetStatus.pending, NbaGame.status == GameStatus.final)
        .distinct()
        .all()
    )

    total = 0
    for (game_id,) in pending_game_ids:
        total += settle_game(db, str(game_id))
    return total

"""
Generic settlement math — no ORM imports.

Works with any game/signal objects that expose:
  game:   .status, .home_score, .away_score
  signal: .market_type, .signal_direction, .line
"""
from __future__ import annotations

from app.modules.basketball.shared.enums import (
    BetStatus,
    GameStatus,
    MarketType,
    SignalDirection,
)


def pnl(odd: float, stake: float, won: bool) -> float:
    """Compute PnL using American odds convention."""
    if not won:
        return -stake
    if odd == 0 or -100 < odd < 0:
        raise ValueError(f"Invalid American odd: {odd!r} (must be >= 100 or <= -100)")
    if odd >= 0:
        return round(stake * odd / 100, 4)
    return round(stake * 100 / abs(odd), 4)


def determine_result(game: object, signal: object) -> BetStatus | None:
    """
    Determine bet result from final game score.

    Returns None when the game is not final or scores are missing.
    Caller is responsible for passing objects with the expected attributes.
    """
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

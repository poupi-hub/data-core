"""
NBA Quant signal rules.

Each setup is a pure function: (game, features, odds) → Signal | None.

Setups implemented:
- HOME_DOG_V1: Home underdog with rest advantage and decent form
- REST_ADVANTAGE_V1: Home team has 2+ more rest days than away
- BACK_TO_BACK_FADE_V1: Away team on back-to-back, take home
- PACE_OVER_V1: Both teams high-pace, take Over
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.modules.nba.quant.models import (
    BetStatus,
    MarketType,
    NbaFeatures,
    NbaGame,
    NbaOdds,
    NbaQuantBet,
    NbaSignal,
    SignalDirection,
)

_STANDARD_ODD = -110.0
_LEAGUE_AVG_PACE = 220.0  # avg total points per game proxy


@dataclass
class SignalResult:
    setup_name: str
    market_type: MarketType
    selection: str
    line: float | None
    odd: float
    direction: SignalDirection
    rationale: str
    confidence: float = 1.0


def _home_ml_odd(odds: list[NbaOdds], home_team: str) -> float | None:
    for o in odds:
        if o.market_type == MarketType.moneyline and o.selection == home_team:
            return float(o.odd)
    return None


def _home_spread(odds: list[NbaOdds], home_team: str) -> tuple[float | None, float]:
    for o in odds:
        if o.market_type == MarketType.spread and o.selection == home_team:
            return float(o.line) if o.line is not None else None, float(o.odd)
    return None, _STANDARD_ODD


def _total_over(odds: list[NbaOdds]) -> tuple[float | None, float]:
    for o in odds:
        if o.market_type == MarketType.totals and "over" in o.selection.lower():
            return float(o.line) if o.line is not None else None, float(o.odd)
    return None, _STANDARD_ODD


# ── Rules ─────────────────────────────────────────────────────────────────────

def _home_dog_v1(
    game: NbaGame, feat: NbaFeatures, odds: list[NbaOdds]
) -> SignalResult | None:
    """
    HOME_DOG_V1: Home team is moneyline underdog (ML > +100),
    has same or more rest than away, and won at least 2 of last 5.
    """
    ml = _home_ml_odd(odds, game.home_team)
    if ml is None or ml <= 100:
        return None
    if feat.home_rest_days is None or feat.away_rest_days is None:
        return None
    if feat.home_rest_days < feat.away_rest_days:
        return None
    if (feat.home_last5_wins or 0) < 2:
        return None

    return SignalResult(
        setup_name="HOME_DOG_V1",
        market_type=MarketType.moneyline,
        selection=game.home_team,
        line=None,
        odd=ml,
        direction=SignalDirection.home,
        rationale=(
            f"Home dog +{ml:.0f}, rest {feat.home_rest_days}v{feat.away_rest_days}, "
            f"L5 {feat.home_last5_wins}/{feat.home_last5_games}"
        ),
    )


def _rest_advantage_v1(
    game: NbaGame, feat: NbaFeatures, odds: list[NbaOdds]
) -> SignalResult | None:
    """
    REST_ADVANTAGE_V1: Home team has 2+ more rest days than away.
    Take home spread.
    """
    if feat.home_rest_days is None or feat.away_rest_days is None:
        return None
    if feat.home_rest_days - feat.away_rest_days < 2:
        return None

    line, odd = _home_spread(odds, game.home_team)

    return SignalResult(
        setup_name="REST_ADVANTAGE_V1",
        market_type=MarketType.spread,
        selection=game.home_team,
        line=line,
        odd=odd,
        direction=SignalDirection.home,
        rationale=(
            f"Home rest {feat.home_rest_days}d vs away {feat.away_rest_days}d "
            f"(+{feat.home_rest_days - feat.away_rest_days}d advantage)"
        ),
    )


def _back_to_back_fade_v1(
    game: NbaGame, feat: NbaFeatures, odds: list[NbaOdds]
) -> SignalResult | None:
    """
    BACK_TO_BACK_FADE_V1: Away team on back-to-back.
    Home team not on B2B. Take home moneyline.
    """
    if not feat.away_back_to_back:
        return None
    if feat.home_back_to_back:
        return None

    ml = _home_ml_odd(odds, game.home_team)
    odd = ml if ml is not None else _STANDARD_ODD
    selection = game.home_team

    return SignalResult(
        setup_name="BACK_TO_BACK_FADE_V1",
        market_type=MarketType.moneyline,
        selection=selection,
        line=None,
        odd=odd,
        direction=SignalDirection.home,
        rationale=(
            f"Away {game.away_team} on B2B, "
            f"home {game.home_team} rested ({feat.home_rest_days}d)"
        ),
    )


def _pace_over_v1(
    game: NbaGame, feat: NbaFeatures, odds: list[NbaOdds]
) -> SignalResult | None:
    """
    PACE_OVER_V1: Both teams above-average pace (proxy: avg total > 220 pts/game).
    Take the Over.
    """
    hp = feat.home_pace
    ap = feat.away_pace
    if hp is None or ap is None:
        return None
    avg_pace = (hp + ap) / 2
    if avg_pace <= _LEAGUE_AVG_PACE:
        return None

    total_line, total_odd = _total_over(odds)

    return SignalResult(
        setup_name="PACE_OVER_V1",
        market_type=MarketType.totals,
        selection=f"Over {total_line}" if total_line else "Over",
        line=total_line,
        odd=total_odd,
        direction=SignalDirection.over,
        rationale=(
            f"Avg pace {avg_pace:.1f} pts/game "
            f"(home {hp:.1f}, away {ap:.1f}) > threshold {_LEAGUE_AVG_PACE}"
        ),
    )


_RULES: list[Callable[[NbaGame, NbaFeatures, list[NbaOdds]], SignalResult | None]] = [
    _home_dog_v1,
    _rest_advantage_v1,
    _back_to_back_fade_v1,
    _pace_over_v1,
]

# ── Orchestration ─────────────────────────────────────────────────────────────

def generate_signals(db: Session, game: NbaGame) -> list[NbaSignal]:
    """Run all rules against a game and persist new signals + paper bets."""
    from app.modules.nba.quant.metrics import nba_q_signals_total

    feat = game.features
    if not feat:
        return []
    odds = game.odds or []

    new_signals: list[NbaSignal] = []

    for rule in _RULES:
        result = rule(game, feat, odds)
        if result is None:
            continue

        existing = (
            db.query(NbaSignal)
            .filter(NbaSignal.game_id == game.id, NbaSignal.setup_name == result.setup_name)
            .first()
        )
        if existing:
            continue

        signal = NbaSignal(
            game_id=game.id,
            setup_name=result.setup_name,
            market_type=result.market_type,
            selection=result.selection,
            line=result.line,
            odd=result.odd,
            signal_direction=result.direction,
            rationale=result.rationale,
            confidence=result.confidence,
        )
        db.add(signal)
        db.flush()

        bet = NbaQuantBet(signal_id=signal.id, stake=1.0, status=BetStatus.pending)
        db.add(bet)

        new_signals.append(signal)
        nba_q_signals_total.labels(setup=result.setup_name).inc()

    db.commit()
    return new_signals


def run_all_games(db: Session) -> int:
    """Generate signals for all games that have features but no signals yet."""
    from app.modules.nba.quant.models import NbaFeatures

    games = (
        db.query(NbaGame)
        .join(NbaFeatures, NbaGame.id == NbaFeatures.game_id)
        .outerjoin(NbaSignal, NbaGame.id == NbaSignal.game_id)
        .filter(NbaSignal.id.is_(None))
        .all()
    )

    count = 0
    for game in games:
        signals = generate_signals(db, game)
        count += len(signals)
    return count

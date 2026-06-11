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
# NOTE: this is NOT true NBA pace (possessions per 48 min, typically 98-102).
# It is the average *total points* per game used as a high-scoring-game proxy.
# A real pace metric requires play-by-play possession data not yet collected.
# Rename makes the approximation explicit to future readers.
_HIGH_SCORING_THRESHOLD = 220.0   # avg total points/game (both teams combined)
_DEFAULT_SPREAD = -3.5            # fallback spread when no real odds available


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


def _home_spread(odds: list[NbaOdds], home_team: str) -> tuple[float, float]:
    """Return (line, odd) for home spread. Falls back to _DEFAULT_SPREAD when no odds."""
    for o in odds:
        if o.market_type == MarketType.spread and o.selection == home_team:
            line = float(o.line) if o.line is not None else _DEFAULT_SPREAD
            return line, float(o.odd)
    return _DEFAULT_SPREAD, _STANDARD_ODD


def _total_over(
    odds: list[NbaOdds], fallback_line: float | None = None
) -> tuple[float | None, float]:
    """Return (line, odd) for Over. Uses fallback_line when no real odds available."""
    for o in odds:
        if o.market_type == MarketType.totals and "over" in o.selection.lower():
            line = float(o.line) if o.line is not None else fallback_line
            return line, float(o.odd)
    return fallback_line, _STANDARD_ODD


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
    # line is guaranteed non-None (defaults to _DEFAULT_SPREAD = -3.5)

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
    """HIGH_SCORING_OVER_V1 (formerly PACE_OVER_V1).

    Both teams have averaged high combined totals in recent games.
    Proxy metric: avg total points per game (NOT true NBA pace / possessions).
    Real pace data requires play-by-play possession counts — not yet available.

    WARNING: home_pace and away_pace are averages over different game samples
    (all home-team games vs all away-team games respectively), so the combined
    average may double-count shared opponents. Treat signal with caution until
    a proper possessions-based pace metric is integrated.
    """
    hp = feat.home_pace
    ap = feat.away_pace
    if hp is None or ap is None:
        return None
    # Simple average of two independent sample means — acknowledged approximation.
    avg_total_proxy = (hp + ap) / 2
    if avg_total_proxy <= _HIGH_SCORING_THRESHOLD:
        return None

    total_line, total_odd = _total_over(odds, fallback_line=round(avg_total_proxy, 1))

    return SignalResult(
        setup_name="PACE_OVER_V1",
        market_type=MarketType.totals,
        selection=f"Over {total_line}",
        line=total_line,
        odd=total_odd,
        direction=SignalDirection.over,
        rationale=(
            f"Avg total proxy {avg_total_proxy:.1f} pts/game "
            f"(home {hp:.1f}, away {ap:.1f}) > threshold {_HIGH_SCORING_THRESHOLD} "
            f"[NOTE: proxy metric, not true pace]"
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

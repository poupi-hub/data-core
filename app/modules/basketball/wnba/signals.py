"""
WNBA Quant signal rules.

Each setup is a pure function: (game, features, odds) → SignalResult | None.

Setups:
  REST_ADVANTAGE_V1   : Home has 2+ more rest days than away → home spread
  HOME_DOG_V1         : Home underdog with rest + form → home moneyline
  BACK_TO_BACK_FADE_V1: Away on B2B, home rested → home moneyline
  TOTAL_PACE_V1       : Both teams high-scoring proxy → Over
  SPREAD_VALUE_V1     : Away heavy favourite (spread > 8.5) → road dog cover

Thresholds are calibrated to WNBA scoring context (~160–180 pts/game combined).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.modules.basketball.shared.enums import (
    BetStatus,
    MarketType,
    SignalDirection,
)
from app.modules.basketball.wnba.models import (
    WnbaFeatures,
    WnbaGame,
    WnbaOdds,
    WnbaQuantBet,
    WnbaSignal,
)

_STANDARD_ODD = -110.0
# WNBA games average ~160–170 combined points; threshold set at 170 for high-pace proxy.
_HIGH_SCORING_THRESHOLD = 170.0
_DEFAULT_SPREAD = -3.5
# Spread gap threshold for SPREAD_VALUE_V1 (heavy road favourite)
_SPREAD_VALUE_THRESHOLD = 8.5


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


def _home_ml_odd(odds: list[WnbaOdds], home_team: str) -> float | None:
    for o in odds:
        if o.market_type == MarketType.moneyline and o.selection == home_team:
            return float(o.odd)
    return None


def _home_spread(odds: list[WnbaOdds], home_team: str) -> tuple[float, float]:
    for o in odds:
        if o.market_type == MarketType.spread and o.selection == home_team:
            line = float(o.line) if o.line is not None else _DEFAULT_SPREAD
            return line, float(o.odd)
    return _DEFAULT_SPREAD, _STANDARD_ODD


def _away_spread(odds: list[WnbaOdds], away_team: str) -> tuple[float | None, float]:
    for o in odds:
        if o.market_type == MarketType.spread and o.selection == away_team:
            line = float(o.line) if o.line is not None else None
            return line, float(o.odd)
    return None, _STANDARD_ODD


def _total_over(
    odds: list[WnbaOdds], fallback_line: float | None = None
) -> tuple[float | None, float]:
    for o in odds:
        if o.market_type == MarketType.totals and "over" in o.selection.lower():
            line = float(o.line) if o.line is not None else fallback_line
            return line, float(o.odd)
    return fallback_line, _STANDARD_ODD


# ── Rules ─────────────────────────────────────────────────────────────────────

def _rest_advantage_v1(
    game: WnbaGame, feat: WnbaFeatures, odds: list[WnbaOdds]
) -> SignalResult | None:
    """Home team has 2+ more rest days than away. Take home spread."""
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


def _home_dog_v1(
    game: WnbaGame, feat: WnbaFeatures, odds: list[WnbaOdds]
) -> SignalResult | None:
    """Home underdog (ML > +100), same-or-more rest, won at least 2 of last 5."""
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


def _back_to_back_fade_v1(
    game: WnbaGame, feat: WnbaFeatures, odds: list[WnbaOdds]
) -> SignalResult | None:
    """Away on B2B, home rested → take home moneyline."""
    if not feat.away_back_to_back:
        return None
    if feat.home_back_to_back:
        return None

    ml = _home_ml_odd(odds, game.home_team)
    odd = ml if ml is not None else _STANDARD_ODD
    return SignalResult(
        setup_name="BACK_TO_BACK_FADE_V1",
        market_type=MarketType.moneyline,
        selection=game.home_team,
        line=None,
        odd=odd,
        direction=SignalDirection.home,
        rationale=(
            f"Away {game.away_team} on B2B, "
            f"home {game.home_team} rested ({feat.home_rest_days}d)"
        ),
    )


def _total_pace_v1(
    game: WnbaGame, feat: WnbaFeatures, odds: list[WnbaOdds]
) -> SignalResult | None:
    """Both teams high-scoring average → Over.

    Uses combined total proxy (home_pace + away_pace) / 2.
    Threshold _HIGH_SCORING_THRESHOLD is calibrated for WNBA scoring (~170 pts).
    """
    hp = feat.home_pace
    ap = feat.away_pace
    if hp is None or ap is None:
        return None
    avg_total_proxy = (hp + ap) / 2
    if avg_total_proxy <= _HIGH_SCORING_THRESHOLD:
        return None

    total_line, total_odd = _total_over(odds, fallback_line=round(avg_total_proxy, 1))
    return SignalResult(
        setup_name="TOTAL_PACE_V1",
        market_type=MarketType.totals,
        selection=f"Over {total_line}",
        line=total_line,
        odd=total_odd,
        direction=SignalDirection.over,
        rationale=(
            f"Avg total proxy {avg_total_proxy:.1f} pts/game "
            f"(home {hp:.1f}, away {ap:.1f}) > {_HIGH_SCORING_THRESHOLD}"
        ),
    )


def _spread_value_v1(
    game: WnbaGame, feat: WnbaFeatures, odds: list[WnbaOdds]
) -> SignalResult | None:
    """Away team is heavy favourite (spread > 8.5) → take home underdog cover.

    Large road favourites in WNBA tend to be overvalued; home teams cover more
    than expected at extreme spreads with a rested home side.
    """
    home_spread_line, _ = _home_spread(odds, game.home_team)
    # home_spread_line is positive when home is underdog (e.g. +9.5)
    if home_spread_line is None or home_spread_line <= _SPREAD_VALUE_THRESHOLD:
        return None
    # Require home to not be on B2B
    if feat.home_back_to_back:
        return None

    _, odd = _home_spread(odds, game.home_team)
    return SignalResult(
        setup_name="SPREAD_VALUE_V1",
        market_type=MarketType.spread,
        selection=game.home_team,
        line=home_spread_line,
        odd=odd,
        direction=SignalDirection.home,
        rationale=(
            f"Home {game.home_team} +{home_spread_line} vs road favourite "
            f"{game.away_team}; large spread value"
        ),
    )


_RULES: list[Callable[[WnbaGame, WnbaFeatures, list[WnbaOdds]], SignalResult | None]] = [
    _rest_advantage_v1,
    _home_dog_v1,
    _back_to_back_fade_v1,
    _total_pace_v1,
    _spread_value_v1,
]

# ── Orchestration ─────────────────────────────────────────────────────────────

def generate_signals(db: Session, game: WnbaGame) -> list[WnbaSignal]:
    """Run all rules against a game and persist new signals + paper bets."""
    from app.modules.basketball.wnba.metrics import wnba_q_signals_total

    feat = game.features
    if not feat:
        return []
    odds = game.odds or []

    new_signals: list[WnbaSignal] = []

    for rule in _RULES:
        result = rule(game, feat, odds)
        if result is None:
            continue

        existing = (
            db.query(WnbaSignal)
            .filter(WnbaSignal.game_id == game.id, WnbaSignal.setup_name == result.setup_name)
            .first()
        )
        if existing:
            continue

        signal = WnbaSignal(
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

        bet = WnbaQuantBet(signal_id=signal.id, stake=1.0, status=BetStatus.pending)
        db.add(bet)

        new_signals.append(signal)
        wnba_q_signals_total.labels(setup=result.setup_name).inc()

    db.commit()
    return new_signals


def run_all_games(db: Session) -> int:
    """Generate signals for all WNBA games that have features but no signals."""
    games = (
        db.query(WnbaGame)
        .join(WnbaFeatures, WnbaGame.id == WnbaFeatures.game_id)
        .outerjoin(WnbaSignal, WnbaGame.id == WnbaSignal.game_id)
        .filter(WnbaSignal.id.is_(None))
        .all()
    )
    count = 0
    for game in games:
        count += len(generate_signals(db, game))
    return count

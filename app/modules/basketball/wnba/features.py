"""
Feature computation for WNBA games.

Same logic as NBA features — rest days, B2B, L5/L10, off/def rating, pace —
but queries wnba_games and wnba_features tables via WnbaGame/WnbaFeatures models.
"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.basketball.shared.enums import GameStatus
from app.modules.basketball.wnba.models import WnbaFeatures, WnbaGame


def _team_recent_games(
    db: Session, team: str, before_date: datetime, limit: int
) -> list[WnbaGame]:
    return (
        db.query(WnbaGame)
        .filter(
            WnbaGame.status == GameStatus.final,
            WnbaGame.game_date < before_date,
            (WnbaGame.home_team == team) | (WnbaGame.away_team == team),
        )
        .order_by(WnbaGame.game_date.desc())
        .limit(limit)
        .all()
    )


def _last_game_date(db: Session, team: str, before_date: datetime) -> datetime | None:
    game = (
        db.query(WnbaGame)
        .filter(
            WnbaGame.status == GameStatus.final,
            WnbaGame.game_date < before_date,
            (WnbaGame.home_team == team) | (WnbaGame.away_team == team),
        )
        .order_by(WnbaGame.game_date.desc())
        .first()
    )
    return game.game_date if game else None


def _wins_in_games(games: list[WnbaGame], team: str) -> int:
    wins = 0
    for g in games:
        if g.home_score is None or g.away_score is None:
            continue
        if g.home_team == team and g.home_score > g.away_score:
            wins += 1
        elif g.away_team == team and g.away_score > g.home_score:
            wins += 1
    return wins


def _avg_scored(games: list[WnbaGame], team: str) -> float | None:
    scores = []
    for g in games:
        if g.home_team == team and g.home_score is not None:
            scores.append(g.home_score)
        elif g.away_team == team and g.away_score is not None:
            scores.append(g.away_score)
    return round(sum(scores) / len(scores), 1) if scores else None


def _avg_allowed(games: list[WnbaGame], team: str) -> float | None:
    allowed = []
    for g in games:
        if g.home_team == team and g.away_score is not None:
            allowed.append(g.away_score)
        elif g.away_team == team and g.home_score is not None:
            allowed.append(g.home_score)
    return round(sum(allowed) / len(allowed), 1) if allowed else None


def _avg_pace(games: list[WnbaGame], team: str) -> float | None:
    totals = []
    for g in games:
        if g.home_score is not None and g.away_score is not None:
            if team in (g.home_team, g.away_team):
                totals.append(g.home_score + g.away_score)
    return round(sum(totals) / len(totals), 1) if totals else None


def compute_features(db: Session, game_id: UUID, _game: WnbaGame | None = None) -> WnbaFeatures | None:
    """Compute and upsert features for a WNBA game. Returns None if game not found."""
    game = _game or db.query(WnbaGame).filter(WnbaGame.id == game_id).first()
    if not game:
        return None

    ref_date = game.game_date

    home_last = _last_game_date(db, game.home_team, ref_date)
    away_last = _last_game_date(db, game.away_team, ref_date)
    home_rest = int((ref_date - home_last).days) if home_last else None
    away_rest = int((ref_date - away_last).days) if away_last else None
    home_b2b = home_rest == 1 if home_rest is not None else False
    away_b2b = away_rest == 1 if away_rest is not None else False

    home_l5 = _team_recent_games(db, game.home_team, ref_date, 5)
    away_l5 = _team_recent_games(db, game.away_team, ref_date, 5)
    home_l10 = _team_recent_games(db, game.home_team, ref_date, 10)
    away_l10 = _team_recent_games(db, game.away_team, ref_date, 10)

    existing = db.query(WnbaFeatures).filter(WnbaFeatures.game_id == game_id).first()
    feat = existing if existing else WnbaFeatures(game_id=game_id)
    if not existing:
        db.add(feat)

    feat.home_rest_days = home_rest
    feat.away_rest_days = away_rest
    feat.home_back_to_back = home_b2b
    feat.away_back_to_back = away_b2b
    feat.home_last5_wins = _wins_in_games(home_l5, game.home_team)
    feat.home_last5_games = len(home_l5)
    feat.away_last5_wins = _wins_in_games(away_l5, game.away_team)
    feat.away_last5_games = len(away_l5)
    feat.home_last10_wins = _wins_in_games(home_l10, game.home_team)
    feat.home_last10_games = len(home_l10)
    feat.away_last10_wins = _wins_in_games(away_l10, game.away_team)
    feat.away_last10_games = len(away_l10)
    feat.home_off_rtg = _avg_scored(home_l10, game.home_team)
    feat.away_off_rtg = _avg_scored(away_l10, game.away_team)
    feat.home_def_rtg = _avg_allowed(home_l10, game.home_team)
    feat.away_def_rtg = _avg_allowed(away_l10, game.away_team)
    feat.home_pace = _avg_pace(home_l10, game.home_team)
    feat.away_pace = _avg_pace(away_l10, game.away_team)
    feat.computed_at = datetime.now(timezone.utc)

    db.commit()
    return feat


def compute_all_pending(db: Session) -> int:
    """Compute features for all final WNBA games without features."""
    games = (
        db.query(WnbaGame)
        .filter(WnbaGame.status == GameStatus.final)
        .outerjoin(WnbaFeatures, WnbaGame.id == WnbaFeatures.game_id)
        .filter(WnbaFeatures.id.is_(None))
        .all()
    )
    count = 0
    for game in games:
        if compute_features(db, game.id, _game=game):
            count += 1
    return count

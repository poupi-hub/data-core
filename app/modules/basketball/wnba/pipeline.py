"""
WNBA Quant pipeline orchestration.

Steps:
  1. Fetch recent games (ESPN WNBA)
  2. Fetch upcoming odds (The Odds API — skipped if key missing)
  3. Compute features
  4. Generate signals + Telegram alerts
  5. Settle pending bets
  6. Refresh edge registry
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.modules.basketball.wnba.analytics import refresh_edge_registry
from app.modules.basketball.wnba.collector import fetch_recent, fetch_season
from app.modules.basketball.wnba.features import compute_all_pending
from app.modules.basketball.wnba.metrics import (
    wnba_q_pipeline_duration_seconds,
    wnba_q_pipeline_runs_total,
)
from app.modules.basketball.wnba.odds_collector import fetch_upcoming_odds
from app.modules.basketball.wnba.paper_betting import settle_all_pending
from app.modules.basketball.wnba.telegram_alerts import send_signal_alert

logger = logging.getLogger(__name__)

_CURRENT_SEASON = 2025
_BACKFILL_SEASONS = [2022, 2023, 2024, 2025]


@dataclass
class PipelineResult:
    started_at: datetime
    finished_at: datetime | None = None
    seasons_fetched: list[int] = field(default_factory=list)
    games_ingested: int = 0
    recent_updated: int = 0
    odds_upserted: int = 0
    odds_blocked: bool = False
    features_computed: int = 0
    signals_generated: int = 0
    alerts_sent: int = 0
    bets_settled: int = 0
    edge_registry_refreshed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def run_full_pipeline(
    db: Session,
    seasons: list[int] | None = None,
    skip_historical: bool = False,
) -> PipelineResult:
    result = PipelineResult(started_at=datetime.now(timezone.utc))
    t0 = time.monotonic()

    logger.info("WNBA quant pipeline starting", extra={"skip_historical": skip_historical})

    if not skip_historical:
        for season in seasons or _BACKFILL_SEASONS:
            try:
                n = fetch_season(db, season)
                result.games_ingested += n
                result.seasons_fetched.append(season)
            except Exception as exc:
                result.errors.append(f"fetch_season({season}): {exc}")

    try:
        result.recent_updated = fetch_recent(db, days_back=7)
    except Exception as exc:
        result.errors.append(f"fetch_recent: {exc}")
        if result.games_ingested == 0:
            result.finished_at = datetime.now(timezone.utc)
            wnba_q_pipeline_runs_total.labels(status="error").inc()
            wnba_q_pipeline_duration_seconds.set(time.monotonic() - t0)
            logger.error("WNBA pipeline aborted: fetch_recent failed with no historical data", extra={"exc": str(exc)})
            return result

    try:
        odds_result = fetch_upcoming_odds(db)
        result.odds_upserted = odds_result.odds_upserted
        result.odds_blocked = odds_result.blocked
    except Exception as exc:
        result.errors.append(f"fetch_upcoming_odds: {exc}")
        result.odds_blocked = True

    try:
        result.features_computed = compute_all_pending(db)
    except Exception as exc:
        result.errors.append(f"compute_all_pending: {exc}")

    try:
        sigs = _run_all_games_with_alerts(db)
        result.signals_generated = sigs["signals"]
        result.alerts_sent = sigs["alerts"]
    except Exception as exc:
        result.errors.append(f"run_all_games: {exc}")

    try:
        result.bets_settled = settle_all_pending(db)
    except Exception as exc:
        result.errors.append(f"settle_all_pending: {exc}")

    try:
        records = refresh_edge_registry(db)
        result.edge_registry_refreshed = len(records)
    except Exception as exc:
        result.errors.append(f"refresh_edge_registry: {exc}")

    result.finished_at = datetime.now(timezone.utc)
    duration = time.monotonic() - t0
    status = "ok" if result.ok else "partial_error"

    wnba_q_pipeline_runs_total.labels(status=status).inc()
    wnba_q_pipeline_duration_seconds.set(duration)

    logger.info(
        "WNBA quant pipeline finished",
        extra={"status": status, "duration_s": round(duration, 2), "errors": result.errors},
    )
    return result


def _run_all_games_with_alerts(db: Session) -> dict:
    from app.modules.basketball.wnba.models import WnbaFeatures, WnbaGame, WnbaSignal
    from app.modules.basketball.wnba.signals import generate_signals

    games = (
        db.query(WnbaGame)
        .join(WnbaFeatures, WnbaGame.id == WnbaFeatures.game_id)
        .outerjoin(WnbaSignal, WnbaGame.id == WnbaSignal.game_id)
        .filter(WnbaSignal.id.is_(None))
        .all()
    )

    total_signals = 0
    total_alerts = 0
    for game in games:
        new_sigs = generate_signals(db, game)
        total_signals += len(new_sigs)
        for sig in new_sigs:
            if send_signal_alert(sig, game, game.features, db=db):
                total_alerts += 1

    return {"signals": total_signals, "alerts": total_alerts}


def run_daily_update(db: Session) -> PipelineResult:
    return run_full_pipeline(db, skip_historical=True)


def run_backfill(db: Session, seasons: list[int] | None = None) -> PipelineResult:
    return run_full_pipeline(db, seasons=seasons, skip_historical=False)

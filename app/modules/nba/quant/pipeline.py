"""
NBA Quant pipeline orchestration.

Full pipeline:
  1. Ingest games (ESPN / BDL)
  2. Fetch upcoming odds (The Odds API — skipped if key missing)
  3. Compute features
  4. Generate signals + send Telegram alerts for B2B
  5. Settle pending bets
  6. Refresh edge registry

Usage:
    from app.modules.nba.quant.pipeline import run_full_pipeline, run_daily_update
    result = run_full_pipeline(db, seasons=[2023, 2024])
    result = run_daily_update(db)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.modules.nba.quant.analytics import refresh_edge_registry
from app.modules.nba.quant.collector import fetch_recent, fetch_season
from app.modules.nba.quant.features import compute_all_pending
from app.modules.nba.quant.metrics import (
    nba_q_pipeline_duration_seconds,
    nba_q_pipeline_runs_total,
)
from app.modules.nba.quant.odds_collector import fetch_upcoming_odds
from app.modules.nba.quant.paper_betting import settle_all_pending
from app.modules.nba.quant.telegram_alerts import send_signal_alert

logger = logging.getLogger(__name__)

# Current active NBA season (2024-25)
_CURRENT_SEASON = 2024
# Historical seasons to backfill (2+ seasons for meaningful edge stats)
_BACKFILL_SEASONS = [2022, 2023, 2024]


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
    """
    Run the complete NBA quant pipeline.

    Steps:
      1. Fetch historical seasons (BDL API) — skipped if skip_historical=True
      2. Fetch recent games (last 7 days) to update live scores
      3. Compute features for all final games without features
      4. Generate signals for all games with features but no signals
      5. Settle pending paper bets for finished games
      6. Refresh edge registry (analytics + classification)

    Args:
        db: SQLAlchemy session
        seasons: seasons to fetch (default: _BACKFILL_SEASONS)
        skip_historical: if True, skip step 1 (use for daily updates)
    """
    result = PipelineResult(started_at=datetime.now(timezone.utc))
    t0 = time.monotonic()

    logger.info(
        "NBA quant pipeline starting",
        extra={
            "skip_historical": skip_historical,
            "seasons": seasons or _BACKFILL_SEASONS,
        },
    )

    # ── Step 1: Historical ingest ─────────────────────────────────────────────
    if not skip_historical:
        target_seasons = seasons or _BACKFILL_SEASONS
        for season in target_seasons:
            try:
                n = fetch_season(db, season)
                result.games_ingested += n
                result.seasons_fetched.append(season)
                logger.info(
                    "BDL season fetched",
                    extra={"season": season, "games": n},
                )
            except Exception as exc:
                msg = f"fetch_season({season}): {exc}"
                logger.error("NBA pipeline step 1 error: %s", msg)
                result.errors.append(msg)

    # ── Step 2: Recent update ─────────────────────────────────────────────────
    try:
        result.recent_updated = fetch_recent(db, days_back=7)
        logger.info("ESPN recent update", extra={"updated": result.recent_updated})
    except Exception as exc:
        msg = f"fetch_recent: {exc}"
        logger.error("NBA pipeline step 2 error: %s", msg)
        result.errors.append(msg)

    # ── Step 2b: Odds — upcoming games (soft step, never blocks pipeline) ─────
    try:
        odds_result = fetch_upcoming_odds(db)
        result.odds_upserted = odds_result.odds_upserted
        result.odds_blocked = odds_result.blocked
        if odds_result.blocked:
            logger.info(
                "Odds fetch blocked (no key or free tier): %s",
                odds_result.blocked_reason,
            )
        elif odds_result.errors:
            logger.warning("Odds fetch partial errors: %s", odds_result.errors[:3])
        else:
            logger.info("Odds updated", extra={"upserted": odds_result.odds_upserted})
    except Exception as exc:
        msg = f"fetch_upcoming_odds: {exc}"
        logger.warning("NBA pipeline step 2b non-fatal: %s", msg)
        result.odds_blocked = True

    # ── Step 3: Features ──────────────────────────────────────────────────────
    try:
        result.features_computed = compute_all_pending(db)
        logger.info("Features computed", extra={"count": result.features_computed})
    except Exception as exc:
        msg = f"compute_all_pending: {exc}"
        logger.error("NBA pipeline step 3 error: %s", msg)
        result.errors.append(msg)

    # ── Step 4: Signals + Telegram alerts ────────────────────────────────────
    try:
        new_signals = _run_all_games_with_alerts(db)
        result.signals_generated = new_signals["signals"]
        result.alerts_sent = new_signals["alerts"]
        logger.info(
            "Signals generated",
            extra={
                "count": result.signals_generated,
                "alerts_sent": result.alerts_sent,
            },
        )
    except Exception as exc:
        msg = f"run_all_games: {exc}"
        logger.error("NBA pipeline step 4 error: %s", msg)
        result.errors.append(msg)

    # ── Step 5: Settle bets ───────────────────────────────────────────────────
    try:
        result.bets_settled = settle_all_pending(db)
        logger.info("Bets settled", extra={"count": result.bets_settled})
    except Exception as exc:
        msg = f"settle_all_pending: {exc}"
        logger.error("NBA pipeline step 5 error: %s", msg)
        result.errors.append(msg)

    # ── Step 6: Edge registry ─────────────────────────────────────────────────
    try:
        records = refresh_edge_registry(db)
        result.edge_registry_refreshed = len(records)
        logger.info("Edge registry refreshed", extra={"setups": result.edge_registry_refreshed})
    except Exception as exc:
        msg = f"refresh_edge_registry: {exc}"
        logger.error("NBA pipeline step 6 error: %s", msg)
        result.errors.append(msg)

    result.finished_at = datetime.now(timezone.utc)
    duration = time.monotonic() - t0
    status = "ok" if result.ok else "partial_error"

    nba_q_pipeline_runs_total.labels(status=status).inc()
    nba_q_pipeline_duration_seconds.set(duration)

    logger.info(
        "NBA quant pipeline finished",
        extra={
            "status": status,
            "duration_s": round(duration, 2),
            "games_ingested": result.games_ingested,
            "odds_upserted": result.odds_upserted,
            "odds_blocked": result.odds_blocked,
            "features_computed": result.features_computed,
            "signals_generated": result.signals_generated,
            "alerts_sent": result.alerts_sent,
            "bets_settled": result.bets_settled,
            "edge_registry_refreshed": result.edge_registry_refreshed,
            "errors": result.errors,
        },
    )
    return result


def _run_all_games_with_alerts(db: Session) -> dict:
    """
    Run signal generation for all pending games and send Telegram alerts
    for new BACK_TO_BACK_FADE_V1 signals.
    Returns {"signals": int, "alerts": int}.
    """
    from app.modules.nba.quant.models import NbaFeatures, NbaGame, NbaSignal

    games = (
        db.query(NbaGame)
        .join(NbaFeatures, NbaGame.id == NbaFeatures.game_id)
        .outerjoin(NbaSignal, NbaGame.id == NbaSignal.game_id)
        .filter(NbaSignal.id.is_(None))
        .all()
    )

    total_signals = 0
    total_alerts = 0

    for game in games:
        from app.modules.nba.quant.signals import generate_signals
        new_sigs = generate_signals(db, game)
        total_signals += len(new_sigs)

        for sig in new_sigs:
            if send_signal_alert(sig, game, game.features, db=db):
                total_alerts += 1

    return {"signals": total_signals, "alerts": total_alerts}


def run_daily_update(db: Session) -> PipelineResult:
    """
    Daily incremental update: skip historical ingest, only update recent games.
    Called by the scheduler every day.
    """
    return run_full_pipeline(db, skip_historical=True)


def run_backfill(db: Session, seasons: list[int] | None = None) -> PipelineResult:
    """
    Full historical backfill. Fetches specified seasons from BDL.
    Default: _BACKFILL_SEASONS (2022, 2023, 2024).
    """
    return run_full_pipeline(db, seasons=seasons, skip_historical=False)

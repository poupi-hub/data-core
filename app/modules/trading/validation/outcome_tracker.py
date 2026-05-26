"""Retrospective signal outcome tracker.

For each TradingAnalytics row with signal BUY or SELL that has not yet been
evaluated, looks forward ``EVALUATION_HORIZON`` candles and records:

  - price_change_pct  — (outcome_close - signal_close) / signal_close × 100
  - max_favorable_pct — highest favourable excursion (MFE) during the window
  - max_adverse_pct   — deepest adverse excursion (MAE) during the window
  - outcome_correct   — True when price moved in the predicted direction

Only processes signals older than ``EVALUATION_HORIZON`` intervals so that the
full horizon window has had time to close.

Prometheus metrics emitted per run:
  - outcome_evaluated_total      [symbol, timeframe, signal, outcome]
  - outcome_skipped_total        [symbol, timeframe]
  - outcome_future_candles_missing_total [symbol, timeframe]
  - outcome_eval_error_total     [symbol, timeframe]
  - outcome_eval_duration_seconds (histogram, no labels)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func
from sqlalchemy.orm import Session

from app.analytics.models import TradingAnalytics
from app.modules.trading.validation.models import TradingSignalOutcome
from app.normalization.models import NormalizedMarketCandle

logger = logging.getLogger(__name__)

# Default number of subsequent candles to evaluate after a signal.
EVALUATION_HORIZON: int = 6

# Map timeframe string → timedelta for computing the horizon cutoff.
_TIMEFRAME_DELTA: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}

# Only evaluate signals within this lookback window (avoid re-scanning ancient history).
_MAX_LOOKBACK_DAYS: int = 30

# ── Lazy Prometheus metric handles ────────────────────────────────────────────
_metrics_loaded = False
_m_evaluated: object = None
_m_skipped: object = None
_m_missing: object = None
_m_error: object = None
_m_duration: object = None


def _load_metrics() -> None:
    global _metrics_loaded, _m_evaluated, _m_skipped, _m_missing, _m_error, _m_duration
    if _metrics_loaded:
        return
    try:
        from api.metrics import (  # noqa: PLC0415
            outcome_eval_duration_seconds,
            outcome_eval_error_total,
            outcome_evaluated_total,
            outcome_future_candles_missing_total,
            outcome_skipped_total,
        )
        _m_evaluated = outcome_evaluated_total
        _m_skipped = outcome_skipped_total
        _m_missing = outcome_future_candles_missing_total
        _m_error = outcome_eval_error_total
        _m_duration = outcome_eval_duration_seconds
        _metrics_loaded = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load Prometheus metrics (non-fatal): %s", exc)


class SignalOutcomeTracker:
    """Evaluates pending BUY/SELL signals and persists their price outcomes.

    Follows the same DB session pattern as other analytics processors
    (SessionLocal handed in from the caller; commit/rollback managed here).
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        _load_metrics()

    def run(self, limit: int = 200) -> dict:
        """Process up to ``limit`` pending signals and return a summary dict."""
        t_start = time.monotonic()
        now = datetime.now(tz=timezone.utc)
        lookback_start = now - timedelta(days=_MAX_LOOKBACK_DAYS)

        # Fetch pending BUY/SELL signals — exclude already-evaluated rows via EXISTS subquery
        # (avoids loading all evaluated IDs into Python memory as the table grows).
        already_evaluated = (
            exists().where(TradingSignalOutcome.analytics_id == TradingAnalytics.id)
        )
        pending = (
            self.db.query(TradingAnalytics)
            .filter(
                TradingAnalytics.signal.in_(["BUY", "SELL"]),
                TradingAnalytics.calculated_at >= lookback_start,
                ~already_evaluated,
            )
            .order_by(TradingAnalytics.calculated_at)
            .limit(limit)
            .all()
        )

        evaluated = 0
        skipped = 0
        errors = 0
        # Counters for Prometheus — accumulate per (symbol, timeframe) during this run.
        _skipped: dict[tuple[str, str], int] = {}
        _missing: dict[tuple[str, str], int] = {}
        _errs: dict[tuple[str, str], int] = {}
        _eval_ok: dict[tuple[str, str, str, str], int] = {}  # (sym, tf, sig, outcome)

        for analytics in pending:
            sym = analytics.symbol or "unknown"
            tf = analytics.timeframe or "unknown"
            sig = analytics.signal or "unknown"
            try:
                outcome, reason = self._evaluate(analytics, now)
                if outcome is None:
                    if reason == "horizon_open":
                        skipped += 1
                        _skipped[(sym, tf)] = _skipped.get((sym, tf), 0) + 1
                    elif reason == "no_candles":
                        skipped += 1
                        _missing[(sym, tf)] = _missing.get((sym, tf), 0) + 1
                    else:
                        # inconclusive (bad price data, etc.)
                        skipped += 1
                        _skipped[(sym, tf)] = _skipped.get((sym, tf), 0) + 1
                    continue
                self.db.add(outcome)
                evaluated += 1
                outcome_label = "correct" if outcome.outcome_correct else "incorrect"
                k = (sym, tf, sig, outcome_label)
                _eval_ok[k] = _eval_ok.get(k, 0) + 1
            except Exception as exc:
                logger.warning(
                    "Signal outcome evaluation failed",
                    extra={"analytics_id": str(analytics.id), "error": str(exc)},
                )
                errors += 1
                _errs[(sym, tf)] = _errs.get((sym, tf), 0) + 1

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        # ── Emit Prometheus metrics ────────────────────────────────────────────
        duration = time.monotonic() - t_start
        self._emit_metrics(_eval_ok, _skipped, _missing, _errs, duration)

        logger.info(
            "Signal outcomes job completed",
            extra={"evaluated": evaluated, "skipped": skipped, "errors": errors,
                   "duration_seconds": round(duration, 3)},
        )
        return {"evaluated": evaluated, "skipped": skipped, "errors": errors}

    # ── Private helpers ────────────────────────────────────────────────────────

    def _evaluate(
        self, analytics: TradingAnalytics, now: datetime
    ) -> tuple[TradingSignalOutcome | None, str]:
        """Compute outcome for one analytics row.

        Returns (outcome_row, reason) where reason describes why outcome is None:
          - "horizon_open"  : horizon window has not yet closed
          - "no_candles"    : no subsequent candles found in DB
          - "bad_price"     : signal or outcome price unavailable/zero
          - (non-None, "")  : success
        """
        timeframe = analytics.timeframe or "1h"
        delta = _TIMEFRAME_DELTA.get(timeframe, timedelta(hours=1))

        calc_at = analytics.calculated_at
        if calc_at.tzinfo is None:
            calc_at = calc_at.replace(tzinfo=timezone.utc)
        horizon_end = calc_at + delta * EVALUATION_HORIZON

        if horizon_end > now:
            return None, "horizon_open"

        candles = (
            self.db.query(NormalizedMarketCandle)
            .filter(
                NormalizedMarketCandle.symbol == analytics.symbol,
                NormalizedMarketCandle.timeframe == timeframe,
                NormalizedMarketCandle.timestamp > calc_at,
                NormalizedMarketCandle.timestamp <= horizon_end,
            )
            .order_by(NormalizedMarketCandle.timestamp)
            .limit(EVALUATION_HORIZON + 2)
            .all()
        )

        if not candles:
            return None, "no_candles"

        # Signal price: try market_candle close, fallback to first horizon candle.
        signal_price: float | None = None
        if analytics.market_candle_id:
            candle_row = self.db.get(NormalizedMarketCandle, analytics.market_candle_id)
            if candle_row and candle_row.close is not None:
                signal_price = float(candle_row.close)
        if signal_price is None and candles:
            signal_price = float(candles[0].close) if candles[0].close else None

        if signal_price is None or signal_price == 0:
            return None, "bad_price"

        outcome_candle = candles[-1]
        outcome_price = float(outcome_candle.close) if outcome_candle.close else None
        if outcome_price is None:
            return None, "bad_price"

        price_change_pct = (outcome_price - signal_price) / signal_price * 100

        highs = [float(c.high) for c in candles if c.high is not None]
        lows = [float(c.low) for c in candles if c.low is not None]
        max_favorable_pct: float | None = None
        max_adverse_pct: float | None = None

        if analytics.signal == "BUY":
            max_favorable_pct = (max(highs) - signal_price) / signal_price * 100 if highs else None
            max_adverse_pct = (min(lows) - signal_price) / signal_price * 100 if lows else None
            outcome_correct = price_change_pct > 0
        else:  # SELL
            max_favorable_pct = (signal_price - min(lows)) / signal_price * 100 if lows else None
            max_adverse_pct = (signal_price - max(highs)) / signal_price * 100 if highs else None
            outcome_correct = price_change_pct < 0

        outcome_at = outcome_candle.timestamp
        if outcome_at is not None and outcome_at.tzinfo is None:
            outcome_at = outcome_at.replace(tzinfo=timezone.utc)

        row = TradingSignalOutcome(
            analytics_id=analytics.id,
            symbol=analytics.symbol,
            timeframe=timeframe,
            signal=analytics.signal,
            confidence=analytics.confidence,
            regime=analytics.regime,
            signal_price=round(signal_price, 8),
            signal_at=calc_at,
            outcome_price=round(outcome_price, 8),
            outcome_at=outcome_at,
            candles_elapsed=len(candles),
            price_change_pct=round(price_change_pct, 4),
            max_favorable_pct=round(max_favorable_pct, 4) if max_favorable_pct is not None else None,
            max_adverse_pct=round(max_adverse_pct, 4) if max_adverse_pct is not None else None,
            outcome_correct=outcome_correct,
            evaluation_horizon_candles=EVALUATION_HORIZON,
            evaluated_at=now,
        )
        return row, ""

    def _emit_metrics(
        self,
        eval_ok: dict[tuple[str, str, str, str], int],
        skipped: dict[tuple[str, str], int],
        missing: dict[tuple[str, str], int],
        errs: dict[tuple[str, str], int],
        duration: float,
    ) -> None:
        if not _metrics_loaded:
            return
        try:
            for (sym, tf, sig, outcome_label), count in eval_ok.items():
                _m_evaluated.labels(  # type: ignore[union-attr]
                    symbol=sym, timeframe=tf, signal=sig, outcome=outcome_label
                ).inc(count)
            for (sym, tf), count in skipped.items():
                _m_skipped.labels(symbol=sym, timeframe=tf).inc(count)  # type: ignore[union-attr]
            for (sym, tf), count in missing.items():
                _m_missing.labels(symbol=sym, timeframe=tf).inc(count)  # type: ignore[union-attr]
            for (sym, tf), count in errs.items():
                _m_error.labels(symbol=sym, timeframe=tf).inc(count)  # type: ignore[union-attr]
            _m_duration.observe(duration)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Prometheus emit failed (non-fatal): %s", exc)

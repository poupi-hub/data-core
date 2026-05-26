"""Candle freshness validation for the crypto/trading pipeline.

Detects stale data, missing intervals, and temporal gaps per symbol/timeframe.
Used by DatasetIntegrityScorer and the dataset_quality_crypto scheduler job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.normalization.models import NormalizedMarketCandle

# Expected collection interval per timeframe (hours).
_TIMEFRAME_HOURS: dict[str, float] = {
    "1m": 1 / 60,
    "3m": 3 / 60,
    "5m": 5 / 60,
    "15m": 0.25,
    "30m": 0.5,
    "1h": 1.0,
    "2h": 2.0,
    "4h": 4.0,
    "6h": 6.0,
    "8h": 8.0,
    "12h": 12.0,
    "1d": 24.0,
}

# A candle is "stale" when its age exceeds this multiplier of the expected interval.
_STALE_MULTIPLIER: float = 2.0

# Window for gap analysis (hours).
_GAP_ANALYSIS_WINDOW_HOURS: int = 24


@dataclass
class FreshnessResult:
    """Freshness report for one symbol/timeframe combination."""

    symbol: str
    timeframe: str
    last_candle_at: datetime | None
    staleness_hours: float
    expected_interval_hours: float
    status: str  # "fresh" | "stale" | "missing"
    gap_count: int  # missing intervals in the last 24h
    candles_in_window: int  # total candles found in the 24h window

    @property
    def is_stale(self) -> bool:
        return self.status in ("stale", "missing")


class CandleFreshnessValidator:
    """Validates temporal freshness of normalized OHLCV candles.

    For each (symbol, timeframe) pair:
    - Finds the most recent candle timestamp.
    - Computes how many hours ago that was (staleness_hours).
    - Classifies as "fresh", "stale", or "missing".
    - Counts gaps in the 24h window (intervals where a candle is absent).
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def check(
        self,
        symbols: list[str],
        timeframes: list[str],
        *,
        source: str | None = None,
    ) -> list[FreshnessResult]:
        """Return a FreshnessResult for every (symbol, timeframe) combination.

        Args:
            symbols: Trading pair symbols to check.
            timeframes: Timeframe strings to check.
            source: When provided, restrict to candles from this source only.
                    Default (None) aggregates across all sources — preserves
                    existing behaviour for the default scheduler job.
        """
        results: list[FreshnessResult] = []
        now = datetime.now(tz=timezone.utc)

        for symbol in symbols:
            for timeframe in timeframes:
                interval_hours = _TIMEFRAME_HOURS.get(timeframe, 1.0)
                result = self._check_one(symbol, timeframe, interval_hours, now, source=source)
                results.append(result)

        return results

    def _check_one(
        self,
        symbol: str,
        timeframe: str,
        interval_hours: float,
        now: datetime,
        *,
        source: str | None = None,
    ) -> FreshnessResult:
        window_start = now - timedelta(hours=_GAP_ANALYSIS_WINDOW_HOURS)

        base_filter = [
            NormalizedMarketCandle.symbol == symbol,
            NormalizedMarketCandle.timeframe == timeframe,
        ]
        if source is not None:
            base_filter.append(NormalizedMarketCandle.source == source)

        # Most recent candle
        last_ts: datetime | None = (
            self.db.query(func.max(NormalizedMarketCandle.timestamp))
            .filter(*base_filter)
            .scalar()
        )

        if last_ts is None:
            return FreshnessResult(
                symbol=symbol,
                timeframe=timeframe,
                last_candle_at=None,
                staleness_hours=float("inf"),
                expected_interval_hours=interval_hours,
                status="missing",
                gap_count=0,
                candles_in_window=0,
            )

        # Normalise to UTC-aware
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        staleness_hours = (now - last_ts).total_seconds() / 3600.0
        status = "fresh" if staleness_hours <= interval_hours * _STALE_MULTIPLIER else "stale"

        # Count candles in the 24h window for gap analysis
        candles_in_window: int = (
            self.db.query(func.count(NormalizedMarketCandle.id))
            .filter(*base_filter, NormalizedMarketCandle.timestamp >= window_start)
            .scalar()
            or 0
        )

        expected_in_window = int(_GAP_ANALYSIS_WINDOW_HOURS / interval_hours)
        gap_count = max(0, expected_in_window - candles_in_window)

        return FreshnessResult(
            symbol=symbol,
            timeframe=timeframe,
            last_candle_at=last_ts,
            staleness_hours=round(staleness_hours, 2),
            expected_interval_hours=interval_hours,
            status=status,
            gap_count=gap_count,
            candles_in_window=candles_in_window,
        )

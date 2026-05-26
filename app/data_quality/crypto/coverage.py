"""Candle coverage analysis for the crypto/trading pipeline.

Measures how many OHLCV candles exist in the last 24 hours vs. how many are expected,
expressing the result as a coverage percentage per symbol/timeframe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.normalization.models import NormalizedMarketCandle

# Expected candle counts per 24 hours — derived from timeframe string.
_EXPECTED_24H: dict[str, int] = {
    "1m": 1440,
    "3m": 480,
    "5m": 288,
    "15m": 96,
    "30m": 48,
    "1h": 24,
    "2h": 12,
    "4h": 6,
    "6h": 4,
    "8h": 3,
    "12h": 2,
    "1d": 1,
}


@dataclass
class CoverageResult:
    """Coverage report for one symbol/timeframe combination."""

    symbol: str
    timeframe: str
    candles_24h: int
    expected_candles_24h: int
    coverage_pct: float  # 0.0-100.0 (may exceed 100 if extras exist)
    oldest_candle_at: datetime | None
    total_candles: int


class CandleCoverageAnalyzer:
    """Analyzes candle coverage for a set of symbol/timeframe pairs.

    Reports how many candles were collected in the last 24 hours relative to what
    is expected given the timeframe interval.  A coverage_pct of 100 means every
    expected candle arrived; anything below signals collection gaps.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def analyze(
        self,
        symbols: list[str],
        timeframes: list[str],
        *,
        source: str | None = None,
    ) -> list[CoverageResult]:
        """Return CoverageResult for every (symbol, timeframe) pair.

        Args:
            symbols: Trading pair symbols to analyze.
            timeframes: Timeframe strings to analyze.
            source: When provided, restrict to candles from this source only.
                    Default (None) aggregates across all sources — preserves
                    existing behaviour for the default scheduler job.
        """
        results: list[CoverageResult] = []
        now = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(hours=24)

        for symbol in symbols:
            for timeframe in timeframes:
                result = self._analyze_one(symbol, timeframe, window_start, source=source)
                results.append(result)

        return results

    def _analyze_one(
        self,
        symbol: str,
        timeframe: str,
        window_start: datetime,
        *,
        source: str | None = None,
    ) -> CoverageResult:
        expected = _EXPECTED_24H.get(timeframe, 24)

        base_filter = [
            NormalizedMarketCandle.symbol == symbol,
            NormalizedMarketCandle.timeframe == timeframe,
        ]
        if source is not None:
            base_filter.append(NormalizedMarketCandle.source == source)

        candles_24h: int = (
            self.db.query(func.count(NormalizedMarketCandle.id))
            .filter(*base_filter, NormalizedMarketCandle.timestamp >= window_start)
            .scalar()
            or 0
        )

        coverage_pct = min(100.0, round(candles_24h / expected * 100, 2)) if expected > 0 else 0.0

        oldest_ts: datetime | None = (
            self.db.query(func.min(NormalizedMarketCandle.timestamp))
            .filter(*base_filter)
            .scalar()
        )
        if oldest_ts is not None and oldest_ts.tzinfo is None:
            oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)

        # Use COUNT with an index-friendly LIMIT hint — avoids full table scan
        # by reusing the same base_filter (indexed on symbol + timeframe).
        total_candles: int = (
            self.db.query(func.count(NormalizedMarketCandle.id))
            .filter(*base_filter)
            .scalar()
            or 0
        )

        return CoverageResult(
            symbol=symbol,
            timeframe=timeframe,
            candles_24h=candles_24h,
            expected_candles_24h=expected,
            coverage_pct=coverage_pct,
            oldest_candle_at=oldest_ts,
            total_candles=total_candles,
        )

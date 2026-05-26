"""Integration tests for CandleFreshnessValidator.

Requires a live PostgreSQL test database (skips if unavailable).
Tests insert synthetic NormalizedMarketCandle rows with known timestamps
and verify that freshness status, staleness_hours, and gap_count are
computed correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.data_quality.crypto.freshness import CandleFreshnessValidator, _STALE_MULTIPLIER
from app.normalization.models import NormalizedMarketCandle


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_candle(
    symbol: str,
    timeframe: str,
    timestamp: datetime,
    source: str = "pytest-freshness",
) -> NormalizedMarketCandle:
    return NormalizedMarketCandle(
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000.0,
        normalizer_name="pytest_normalizer",
        normalizer_version="1.0.0",
    )


# ── tests ─────────────────────────────────────────────────────────────────────

def test_fresh_candle_returns_fresh_status(db_session):
    """A candle younger than 2× the interval must be classified as 'fresh'."""
    symbol = f"SOL/USDT"
    timeframe = "1h"
    source = f"pytest-freshness-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # Insert a candle 30 minutes ago — well within 2h stale threshold for 1h TF
    candle = _make_candle(symbol, timeframe, now - timedelta(minutes=30), source=source)
    db_session.add(candle)
    db_session.flush()

    validator = CandleFreshnessValidator(db_session)
    results = validator.check([symbol], [timeframe])

    assert len(results) == 1
    result = results[0]
    assert result.symbol == symbol
    assert result.timeframe == timeframe
    assert result.status == "fresh"
    assert result.staleness_hours < 1.0  # < 60 minutes
    assert result.last_candle_at is not None


def test_stale_candle_returns_stale_status(db_session):
    """A candle older than 2× interval must be classified as 'stale'."""
    symbol = "DOGE/USDT"
    timeframe = "1h"
    source = f"pytest-freshness-stale-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # Insert a candle 5 hours ago — exceeds 2h stale threshold for 1h TF
    candle = _make_candle(symbol, timeframe, now - timedelta(hours=5), source=source)
    db_session.add(candle)
    db_session.flush()

    validator = CandleFreshnessValidator(db_session)
    results = validator.check([symbol], [timeframe])

    assert results[0].status == "stale"
    assert results[0].staleness_hours >= 5.0
    assert results[0].is_stale is True


def test_missing_symbol_returns_missing_status(db_session):
    """A symbol with zero candles must return status='missing' and staleness=inf."""
    symbol = f"MISSING/USDT"
    timeframe = "1h"
    # Do NOT insert any candles for this symbol

    validator = CandleFreshnessValidator(db_session)
    results = validator.check([symbol], [timeframe])

    assert results[0].status == "missing"
    assert results[0].staleness_hours == float("inf")
    assert results[0].last_candle_at is None
    assert results[0].is_stale is True


def test_gap_count_with_missing_intervals(db_session):
    """gap_count should equal the number of missing 1h intervals in the last 24h."""
    symbol = "XRP/USDT"
    timeframe = "1h"
    source = f"pytest-freshness-gaps-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # Insert only 12 candles in the last 24h (should have 24 → gap_count = 12)
    for i in range(12):
        db_session.add(_make_candle(symbol, timeframe, now - timedelta(hours=i + 1), source=source))
    db_session.flush()

    validator = CandleFreshnessValidator(db_session)
    results = validator.check([symbol], [timeframe])

    result = results[0]
    assert result.candles_in_window == 12
    assert result.gap_count == 12  # 24 expected − 12 found


def test_no_gaps_when_all_intervals_present(db_session):
    """gap_count should be 0 when all expected 1h candles exist in the 24h window."""
    symbol = "BTC/USDT"
    timeframe = "1h"
    source = f"pytest-freshness-nogaps-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # Insert exactly 24 candles — one per hour for the past 24h
    for i in range(24):
        db_session.add(_make_candle(symbol, timeframe, now - timedelta(hours=i + 0.5), source=source))
    db_session.flush()

    validator = CandleFreshnessValidator(db_session)
    results = validator.check([symbol], [timeframe])

    result = results[0]
    assert result.candles_in_window == 24
    assert result.gap_count == 0


def test_multiple_symbols_checked_independently(db_session):
    """check() should return one FreshnessResult per (symbol, timeframe) pair."""
    source = f"pytest-freshness-multi-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    symbols = ["SOL/USDT", "DOGE/USDT"]
    timeframe = "1h"

    # SOL: fresh (30 min ago)
    db_session.add(_make_candle("SOL/USDT", timeframe, now - timedelta(minutes=30), source=source))
    # DOGE: stale (6 hours ago)
    db_session.add(_make_candle("DOGE/USDT", timeframe, now - timedelta(hours=6), source=source))
    db_session.flush()

    validator = CandleFreshnessValidator(db_session)
    results = validator.check(symbols, [timeframe])

    assert len(results) == 2
    by_symbol = {r.symbol: r for r in results}

    assert by_symbol["SOL/USDT"].status == "fresh"
    assert by_symbol["DOGE/USDT"].status == "stale"


def test_15m_timeframe_stale_threshold_is_30_minutes(db_session):
    """For 15m candles, stale threshold = 2 × 15min = 30min."""
    symbol = "ETH/USDT"
    timeframe = "15m"
    source = f"pytest-freshness-15m-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # 10 minutes ago — should be fresh (< 30 min threshold)
    db_session.add(_make_candle(symbol, timeframe, now - timedelta(minutes=10), source=source))
    db_session.flush()

    validator = CandleFreshnessValidator(db_session)
    results = validator.check([symbol], [timeframe])

    assert results[0].status == "fresh"
    assert results[0].expected_interval_hours == 0.25


def test_freshness_result_expected_interval_matches_timeframe(db_session):
    """expected_interval_hours must match the canonical _TIMEFRAME_HOURS mapping."""
    symbol = "BTC/USDT"
    timeframe = "4h"
    source = f"pytest-freshness-interval-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    db_session.add(_make_candle(symbol, timeframe, now - timedelta(hours=1), source=source))
    db_session.flush()

    validator = CandleFreshnessValidator(db_session)
    results = validator.check([symbol], [timeframe])

    assert results[0].expected_interval_hours == 4.0

"""Integration tests for CandleCoverageAnalyzer and DatasetIntegrityScorer.

Requires a live PostgreSQL test database (skips if unavailable).
Tests insert synthetic NormalizedMarketCandle rows and verify that coverage
percentages and integrity scores are computed as expected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.data_quality.crypto.coverage import CandleCoverageAnalyzer, _EXPECTED_24H
from app.data_quality.crypto.integrity import DatasetIntegrityScorer
from app.normalization.models import NormalizedMarketCandle


# ── helpers ───────────────────────────────────────────────────────────────────

def _insert_candles(
    db_session,
    symbol: str,
    timeframe: str,
    count: int,
    source: str,
    offset_hours: float = 0.5,
) -> None:
    """Insert ``count`` candles spread over the last 24 hours."""
    now = datetime.now(tz=timezone.utc)
    for i in range(count):
        ts = now - timedelta(hours=offset_hours + i * (24.0 / max(count, 1)))
        db_session.add(
            NormalizedMarketCandle(
                source=source,
                symbol=symbol,
                timeframe=timeframe,
                timestamp=ts,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
                normalizer_name="pytest_normalizer",
                normalizer_version="1.0.0",
            )
        )
    db_session.flush()


# ── CandleCoverageAnalyzer tests ──────────────────────────────────────────────

def test_zero_candles_yields_zero_coverage(db_session):
    symbol = f"NOCOVERAGE/USDT"
    timeframe = "1h"
    # No candles inserted

    analyzer = CandleCoverageAnalyzer(db_session)
    results = analyzer.analyze([symbol], [timeframe])

    assert len(results) == 1
    result = results[0]
    assert result.candles_24h == 0
    assert result.coverage_pct == 0.0
    assert result.expected_candles_24h == 24


def test_full_coverage_24_candles_1h(db_session):
    """24 candles in 24h for 1h timeframe = 100% coverage."""
    symbol = "BTC/USDT"
    timeframe = "1h"
    source = f"pytest-cov-full-{uuid4().hex[:8]}"

    _insert_candles(db_session, symbol, timeframe, 24, source)

    analyzer = CandleCoverageAnalyzer(db_session)
    results = analyzer.analyze([symbol], [timeframe])

    result = results[0]
    assert result.candles_24h == 24
    assert result.coverage_pct == 100.0
    assert result.expected_candles_24h == 24


def test_partial_coverage_12_candles_1h(db_session):
    """12 candles in 24h for 1h timeframe = 50% coverage."""
    symbol = "ETH/USDT"
    timeframe = "1h"
    source = f"pytest-cov-partial-{uuid4().hex[:8]}"

    _insert_candles(db_session, symbol, timeframe, 12, source)

    analyzer = CandleCoverageAnalyzer(db_session)
    results = analyzer.analyze([symbol], [timeframe])

    result = results[0]
    assert result.candles_24h == 12
    assert result.coverage_pct == 50.0


def test_coverage_capped_at_100_with_extra_candles(db_session):
    """Extra candles beyond expected should not push coverage above 100%."""
    symbol = "SOL/USDT"
    timeframe = "1h"
    source = f"pytest-cov-extra-{uuid4().hex[:8]}"

    # Insert 30 candles when only 24 are expected
    _insert_candles(db_session, symbol, timeframe, 30, source, offset_hours=0.1)

    analyzer = CandleCoverageAnalyzer(db_session)
    results = analyzer.analyze([symbol], [timeframe])

    result = results[0]
    assert result.coverage_pct == 100.0  # capped at 100


def test_coverage_15m_expected_96_candles(db_session):
    """15m timeframe expects 96 candles per 24h."""
    symbol = "DOGE/USDT"
    timeframe = "15m"
    source = f"pytest-cov-15m-{uuid4().hex[:8]}"

    _insert_candles(db_session, symbol, timeframe, 48, source, offset_hours=0.1)

    analyzer = CandleCoverageAnalyzer(db_session)
    results = analyzer.analyze([symbol], [timeframe])

    result = results[0]
    assert result.expected_candles_24h == 96
    assert result.coverage_pct == 50.0
    assert result.candles_24h == 48


def test_coverage_multiple_symbols(db_session):
    """analyze() returns one CoverageResult per (symbol, timeframe) pair."""
    source_btc = f"pytest-cov-btc-{uuid4().hex[:8]}"
    source_eth = f"pytest-cov-eth-{uuid4().hex[:8]}"
    timeframe = "1h"

    _insert_candles(db_session, "BTC/USDT", timeframe, 24, source_btc)
    _insert_candles(db_session, "ETH/USDT", timeframe, 12, source_eth)

    analyzer = CandleCoverageAnalyzer(db_session)
    results = analyzer.analyze(["BTC/USDT", "ETH/USDT"], [timeframe])

    assert len(results) == 2
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["BTC/USDT"].coverage_pct == 100.0
    assert by_symbol["ETH/USDT"].coverage_pct == 50.0


def test_total_candles_includes_older_data(db_session):
    """total_candles should count ALL candles, not just the 24h window."""
    symbol = "XRP/USDT"
    timeframe = "1h"
    source = f"pytest-cov-total-{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)

    # 10 candles inside 24h window
    for i in range(10):
        db_session.add(NormalizedMarketCandle(
            source=source, symbol=symbol, timeframe=timeframe,
            timestamp=now - timedelta(hours=i + 1),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            normalizer_name="pytest_normalizer", normalizer_version="1.0.0",
        ))
    # 5 candles older than 24h
    for i in range(5):
        db_session.add(NormalizedMarketCandle(
            source=source, symbol=symbol, timeframe=timeframe,
            timestamp=now - timedelta(hours=25 + i),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            normalizer_name="pytest_normalizer", normalizer_version="1.0.0",
        ))
    db_session.flush()

    analyzer = CandleCoverageAnalyzer(db_session)
    results = analyzer.analyze([symbol], [timeframe])

    result = results[0]
    assert result.candles_24h == 10
    assert result.total_candles >= 15  # may include other pytest data, but >= 15


# ── DatasetIntegrityScorer tests ──────────────────────────────────────────────

def test_integrity_scorer_returns_score_between_0_and_100(db_session):
    """DatasetIntegrityScorer.score() must produce scores in [0, 100]."""
    symbol = "SOL/USDT"
    timeframe = "1h"
    source = f"pytest-integrity-{uuid4().hex[:8]}"

    _insert_candles(db_session, symbol, timeframe, 20, source)

    scorer = DatasetIntegrityScorer(db_session)
    results = scorer.score([symbol], [timeframe], persist=False, emit_metrics=False)

    assert len(results) == 1
    score = results[0]
    assert 0.0 <= score.score <= 100.0
    assert 0.0 <= score.freshness_score <= 40.0
    assert 0.0 <= score.coverage_score <= 40.0
    assert 0.0 <= score.ohlc_score <= 20.0


def test_integrity_scorer_full_dataset_scores_high(db_session):
    """A symbol with 24 recent candles should score > 60/100."""
    symbol = "BTC/USDT"
    timeframe = "1h"
    source = f"pytest-integrity-high-{uuid4().hex[:8]}"

    _insert_candles(db_session, symbol, timeframe, 24, source, offset_hours=0.1)

    scorer = DatasetIntegrityScorer(db_session)
    results = scorer.score([symbol], [timeframe], persist=False, emit_metrics=False)

    assert results[0].score > 60.0


def test_integrity_scorer_missing_data_scores_zero_freshness(db_session):
    """A symbol with no candles should have freshness_score = 0."""
    symbol = f"MISSING/USDT"
    timeframe = "1h"

    scorer = DatasetIntegrityScorer(db_session)
    results = scorer.score([symbol], [timeframe], persist=False, emit_metrics=False)

    assert results[0].freshness_score == 0.0
    assert results[0].score < 25.0  # very low total


def test_integrity_scorer_components_dict_has_expected_keys(db_session):
    """IntegrityScore.components must include freshness, coverage, ohlc keys."""
    symbol = "DOGE/USDT"
    timeframe = "1h"
    source = f"pytest-integrity-comp-{uuid4().hex[:8]}"

    _insert_candles(db_session, symbol, timeframe, 12, source)

    scorer = DatasetIntegrityScorer(db_session)
    results = scorer.score([symbol], [timeframe], persist=False, emit_metrics=False)

    components = results[0].components
    assert "freshness" in components
    assert "coverage" in components
    assert "ohlc" in components


def test_integrity_scorer_multiple_pairs(db_session):
    """score() should return one IntegrityScore per (symbol, timeframe) pair."""
    source = f"pytest-integrity-multi-{uuid4().hex[:8]}"
    _insert_candles(db_session, "SOL/USDT", "1h", 20, source)
    _insert_candles(db_session, "DOGE/USDT", "1h", 5, source)

    scorer = DatasetIntegrityScorer(db_session)
    results = scorer.score(["SOL/USDT", "DOGE/USDT"], ["1h"], persist=False, emit_metrics=False)

    assert len(results) == 2
    by_symbol = {r.symbol: r for r in results}
    # SOL with more candles should score higher than DOGE
    assert by_symbol["SOL/USDT"].score >= by_symbol["DOGE/USDT"].score

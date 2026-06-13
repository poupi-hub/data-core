"""Tests for Phase 6B: Performance Guard."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.auto_healing.performance_guard import (
    LATENCY_GO_S,
    LATENCY_HIGH_S,
    LATENCY_WARN_S,
    JSONL_PROJECTION_WARN_MB,
    CACHE_HIT_WARN_RATIO,
    HOT_PATH_MAX_READS,
    SEVERITY_GO,
    SEVERITY_WARN,
    SEVERITY_HIGH,
    SEVERITY_CRITICAL,
    _classify_latency,
    _max_sev,
    CacheHealthMonitor,
    DigestBenchmark,
    FileGrowthWatchdog,
    HotPathDetector,
    PerformanceGuard,
    PerformanceReporter,
)
from app.auto_healing.analytics import HistoryReader


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_counters():
    HistoryReader._cache_hits = 0
    HistoryReader._cache_misses = 0


# ── _classify_latency ─────────────────────────────────────────────────────────

class TestClassifyLatency:
    def test_go_below_threshold(self):
        assert _classify_latency(0.5) == SEVERITY_GO
        assert _classify_latency(LATENCY_GO_S - 0.01) == SEVERITY_GO

    def test_warn_boundary(self):
        assert _classify_latency(LATENCY_GO_S) == SEVERITY_WARN
        assert _classify_latency(3.0) == SEVERITY_WARN
        assert _classify_latency(LATENCY_WARN_S - 0.01) == SEVERITY_WARN

    def test_high_boundary(self):
        assert _classify_latency(LATENCY_WARN_S) == SEVERITY_HIGH
        assert _classify_latency(6.0) == SEVERITY_HIGH
        assert _classify_latency(LATENCY_HIGH_S - 0.01) == SEVERITY_HIGH

    def test_critical_at_threshold(self):
        assert _classify_latency(LATENCY_HIGH_S) == SEVERITY_CRITICAL
        assert _classify_latency(10.0) == SEVERITY_CRITICAL


# ── _max_sev ──────────────────────────────────────────────────────────────────

class TestMaxSev:
    def test_returns_highest(self):
        assert _max_sev(SEVERITY_GO, SEVERITY_CRITICAL) == SEVERITY_CRITICAL
        assert _max_sev(SEVERITY_WARN, SEVERITY_HIGH) == SEVERITY_HIGH
        assert _max_sev(SEVERITY_GO, SEVERITY_GO) == SEVERITY_GO

    def test_single_value(self):
        assert _max_sev(SEVERITY_WARN) == SEVERITY_WARN


# ── PerformanceGuard ──────────────────────────────────────────────────────────

class TestPerformanceGuard:
    def test_save_and_load_history(self, tmp_path):
        guard = PerformanceGuard(str(tmp_path / "auto_healing_watchdog.jsonl"))
        guard.save({"timestamp": "2026-06-13T10:00:00+00:00", "latency_s": 1.5, "severity": "GO"})
        guard.save({"timestamp": "2026-06-12T10:00:00+00:00", "latency_s": 2.5, "severity": "WARN"})
        history = guard.load_history(days=7)
        assert len(history) == 2

    def test_load_filters_old_entries(self, tmp_path):
        guard = PerformanceGuard(str(tmp_path / "auto_healing_watchdog.jsonl"))
        guard.save({"timestamp": "2020-01-01T00:00:00+00:00", "latency_s": 9.0, "severity": "CRITICAL"})
        guard.save({"timestamp": "2026-06-13T10:00:00+00:00", "latency_s": 1.0, "severity": "GO"})
        history = guard.load_history(days=7)
        assert len(history) == 1
        assert history[0]["latency_s"] == 1.0

    def test_compute_7d_stats_empty(self, tmp_path):
        guard = PerformanceGuard(str(tmp_path / "auto_healing_watchdog.jsonl"))
        avg, trend = guard.compute_7d_stats()
        assert avg is None
        assert trend == "insufficient_data"

    def test_compute_7d_stats_stable(self, tmp_path):
        guard = PerformanceGuard(str(tmp_path / "auto_healing_watchdog.jsonl"))
        for i in range(6):
            guard.save({
                "timestamp": f"2026-06-{7+i:02d}T10:00:00+00:00",
                "latency_s": 1.5,
                "severity": "GO",
            })
        avg, trend = guard.compute_7d_stats()
        assert avg == 1.5
        assert trend == "stable"

    def test_compute_7d_stats_degrading(self, tmp_path):
        guard = PerformanceGuard(str(tmp_path / "auto_healing_watchdog.jsonl"))
        # First 3 fast, last 3 slow
        for i, lat in enumerate([1.0, 1.1, 1.2, 3.0, 4.0, 5.0]):
            guard.save({
                "timestamp": f"2026-06-{7+i:02d}T10:00:00+00:00",
                "latency_s": lat,
                "severity": "GO",
            })
        _, trend = guard.compute_7d_stats()
        assert trend == "degrading"

    def test_measure_uses_executive_reporter(self, tmp_path):
        guard = PerformanceGuard(str(tmp_path / "auto_healing_watchdog.jsonl"))
        mock_report = MagicMock()
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER, \
             patch("app.auto_healing.analytics.HistoryReader"):
            MockER.return_value.generate.return_value = mock_report
            latency_s, severity, error = guard.measure(window_hours=24)
        assert latency_s is not None
        assert latency_s >= 0
        assert error is None

    def test_measure_returns_critical_on_exception(self, tmp_path):
        guard = PerformanceGuard(str(tmp_path / "auto_healing_watchdog.jsonl"))
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER:
            MockER.return_value.generate.side_effect = RuntimeError("DB down")
            latency_s, severity, error = guard.measure(window_hours=24)
        assert latency_s is None
        assert severity == SEVERITY_CRITICAL
        assert "DB down" in (error or "")


# ── FileGrowthWatchdog ────────────────────────────────────────────────────────

class TestFileGrowthWatchdog:
    def _make_watchdog(self, tmp_path):
        watchdog_jsonl = tmp_path / "auto_healing_watchdog.jsonl"
        watchdog_jsonl.write_text("")
        return FileGrowthWatchdog(str(watchdog_jsonl))

    def test_measure_returns_records_for_jsonl_files(self, tmp_path):
        (tmp_path / "auto_healing_watchdog.jsonl").write_bytes(b"x" * 1024 * 512)  # 0.5 MB
        watcher = FileGrowthWatchdog(str(tmp_path / "auto_healing_watchdog.jsonl"))
        records = watcher.measure()
        assert len(records) >= 1
        assert records[0].size_mb > 0

    def test_no_growth_on_first_run(self, tmp_path):
        (tmp_path / "auto_healing_watchdog.jsonl").write_bytes(b"x" * 100)
        watcher = FileGrowthWatchdog(str(tmp_path / "auto_healing_watchdog.jsonl"))
        records = watcher.measure()
        # First run: snapshot saved and immediately re-read → growth = 0.0 (same file)
        # No prior snapshot from a different time → projection = 0 → GO
        assert records[0].severity == SEVERITY_GO

    def test_go_severity_for_small_file(self, tmp_path):
        (tmp_path / "auto_healing_watchdog.jsonl").write_bytes(b"x" * 1024 * 100)
        watcher = FileGrowthWatchdog(str(tmp_path / "auto_healing_watchdog.jsonl"))
        records = watcher.measure()
        # No prior data → no projection → GO
        assert records[0].severity == SEVERITY_GO

    def test_to_dict_rounds_fields(self, tmp_path):
        (tmp_path / "auto_healing_watchdog.jsonl").write_bytes(b"x" * 1000)
        watcher = FileGrowthWatchdog(str(tmp_path / "auto_healing_watchdog.jsonl"))
        records = watcher.measure()
        d = records[0].to_dict()
        assert isinstance(d["size_mb"], float)


# ── HotPathDetector ───────────────────────────────────────────────────────────

class TestHotPathDetector:
    def setup_method(self):
        _reset_counters()

    def test_go_below_threshold(self):
        detector = HotPathDetector()
        sev, msg = detector.check(3)
        assert sev == SEVERITY_GO
        assert msg is None

    def test_high_above_threshold(self):
        detector = HotPathDetector()
        sev, msg = detector.check(HOT_PATH_MAX_READS + 1)
        assert sev == SEVERITY_HIGH
        assert msg is not None
        assert str(HOT_PATH_MAX_READS + 1) in msg

    def test_snapshot_reads_class_counters(self):
        HistoryReader._cache_hits = 10
        HistoryReader._cache_misses = 4
        detector = HotPathDetector()
        hits, misses, reads = detector.snapshot_and_reset()
        assert hits == 10
        assert misses == 4
        assert reads == 4
        # Counters reset
        assert HistoryReader._cache_hits == 0
        assert HistoryReader._cache_misses == 0


# ── DigestBenchmark ───────────────────────────────────────────────────────────

class TestDigestBenchmark:
    def setup_method(self):
        _reset_counters()

    def test_run_with_mock_reporter(self):
        bench = DigestBenchmark()
        mock_report = MagicMock()
        mock_report.to_dict.return_value = {}
        mock_report.to_telegram.return_value = "text"
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER, \
             patch("app.auto_healing.analytics.HistoryReader"):
            MockER.return_value.generate.return_value = mock_report
            result = bench.run(window_hours=24)
        assert result.fetch_s is not None
        assert result.render_s is not None
        assert result.telegram_s is not None
        assert result.total_s >= 0
        assert result.error is None

    def test_run_captures_error(self):
        bench = DigestBenchmark()
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER:
            MockER.return_value.generate.side_effect = RuntimeError("fail")
            result = bench.run(window_hours=24)
        assert result.error is not None
        assert result.fetch_s is None
        assert result.severity == SEVERITY_CRITICAL

    def test_to_dict_rounds_fields(self):
        bench = DigestBenchmark()
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER, \
             patch("app.auto_healing.analytics.HistoryReader"):
            MockER.return_value.generate.return_value = MagicMock(
                to_dict=lambda: {}, to_telegram=lambda mode: ""
            )
            result = bench.run()
        d = result.to_dict()
        assert isinstance(d["total_s"], float)


# ── CacheHealthMonitor ────────────────────────────────────────────────────────

class TestCacheHealthMonitor:
    def setup_method(self):
        _reset_counters()

    def test_no_calls_returns_none_ratio(self):
        monitor = CacheHealthMonitor()
        hits, misses, ratio, sev = monitor.collect()
        assert hits == 0
        assert misses == 0
        assert ratio is None
        assert sev == SEVERITY_GO

    def test_high_hit_ratio_is_go(self):
        HistoryReader._cache_hits = 9
        HistoryReader._cache_misses = 1
        monitor = CacheHealthMonitor()
        _, _, ratio, sev = monitor.collect()
        assert ratio == 0.9
        assert sev == SEVERITY_GO

    def test_low_hit_ratio_is_warn(self):
        HistoryReader._cache_hits = 2
        HistoryReader._cache_misses = 8
        monitor = CacheHealthMonitor()
        _, _, ratio, sev = monitor.collect()
        assert ratio == 0.2
        assert sev == SEVERITY_WARN

    def test_exactly_at_threshold_is_go(self):
        # 70% = GO boundary
        HistoryReader._cache_hits = 7
        HistoryReader._cache_misses = 3
        monitor = CacheHealthMonitor()
        _, _, ratio, sev = monitor.collect()
        assert ratio is not None
        assert ratio >= CACHE_HIT_WARN_RATIO
        assert sev == SEVERITY_GO


# ── PerformanceReporter (integration-lite) ────────────────────────────────────

class TestPerformanceReporter:
    def setup_method(self):
        _reset_counters()

    def test_run_returns_report(self, tmp_path):
        (tmp_path / "auto_healing_watchdog.jsonl").write_text("")
        reporter = PerformanceReporter(str(tmp_path / "auto_healing_watchdog.jsonl"))
        mock_report = MagicMock()
        mock_report.to_dict.return_value = {}
        mock_report.to_telegram.return_value = ""
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER, \
             patch("app.auto_healing.analytics.HistoryReader", wraps=HistoryReader):
            MockER.return_value.generate.return_value = mock_report
            report = reporter.run(window_hours=24)
        assert report.generated_at
        assert report.verdict in (SEVERITY_GO, SEVERITY_WARN, SEVERITY_HIGH, SEVERITY_CRITICAL)
        assert isinstance(report.findings, list)

    def test_verdict_escalates_on_high_latency(self, tmp_path):
        (tmp_path / "auto_healing_watchdog.jsonl").write_text("")
        reporter = PerformanceReporter(str(tmp_path / "auto_healing_watchdog.jsonl"))
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER, \
             patch("app.auto_healing.analytics.HistoryReader"):
            # Simulate slow generate (>8s)
            def slow_generate(*a, **kw):
                time.sleep(0.01)  # tiny in tests; we mock latency instead
                return MagicMock(to_dict=lambda: {}, to_telegram=lambda mode: "")
            MockER.return_value.generate.side_effect = slow_generate
            # Patch _classify_latency to force CRITICAL
            with patch("app.auto_healing.performance_guard._classify_latency", return_value=SEVERITY_CRITICAL):
                report = reporter.run(window_hours=24)
        assert report.verdict == SEVERITY_CRITICAL
        assert any(f["severity"] == SEVERITY_CRITICAL for f in report.findings)

    def test_to_telegram_includes_all_sections(self, tmp_path):
        (tmp_path / "auto_healing_watchdog.jsonl").write_text("")
        reporter = PerformanceReporter(str(tmp_path / "auto_healing_watchdog.jsonl"))
        with patch("app.auto_healing.intelligence.ExecutiveReporter") as MockER, \
             patch("app.auto_healing.analytics.HistoryReader"):
            MockER.return_value.generate.return_value = MagicMock(
                to_dict=lambda: {}, to_telegram=lambda mode: ""
            )
            report = reporter.run(window_hours=24)
        text = report.to_telegram()
        assert "Performance Guard" in text
        assert "Verdict" in text
        assert "Latência" in text


# ── HistoryReader counter instrumentation ─────────────────────────────────────

class TestHistoryReaderCounters:
    def setup_method(self):
        _reset_counters()

    def test_cache_miss_increments_on_first_read(self, tmp_path):
        path = tmp_path / "test.jsonl"
        path.write_text("")
        reader = HistoryReader(str(path))
        reader.read_entries(window_hours=24)
        assert HistoryReader._cache_misses == 1
        assert HistoryReader._cache_hits == 0

    def test_cache_hit_increments_on_second_read(self, tmp_path):
        path = tmp_path / "test.jsonl"
        path.write_text("")
        reader = HistoryReader(str(path))
        reader.read_entries(window_hours=24)
        reader.read_entries(window_hours=24)
        assert HistoryReader._cache_misses == 1
        assert HistoryReader._cache_hits == 1

    def test_different_windows_both_miss(self, tmp_path):
        path = tmp_path / "test.jsonl"
        path.write_text("")
        reader = HistoryReader(str(path))
        reader.read_entries(window_hours=24)
        reader.read_entries(window_hours=168)
        assert HistoryReader._cache_misses == 2
        assert HistoryReader._cache_hits == 0

    def test_counters_are_class_level(self, tmp_path):
        path = tmp_path / "test.jsonl"
        path.write_text("")
        r1 = HistoryReader(str(path))
        r2 = HistoryReader(str(path))
        r1.read_entries(window_hours=24)
        r2.read_entries(window_hours=24)  # different instance, same window → each misses once
        assert HistoryReader._cache_misses == 2  # each instance has its own cache

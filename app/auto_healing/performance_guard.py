"""Phase 6B: Performance Guard — proactive degradation detection.

Monitors the executive-summary computation latency, JSONL file growth,
cache effectiveness, hot-path read frequency, and runs synthetic digest
benchmarks to detect degradation days before it becomes an incident.

Five monitors:
  1. PerformanceGuard    — latency measurement + daily history
  2. FileGrowthWatchdog  — JSONL growth + 30-day projection
  3. HotPathDetector     — excessive file reads per request
  4. DigestBenchmark     — synthetic fetch/render/telegram timing
  5. CacheHealthMonitor  — hit/miss ratio from HistoryReader

Output:
  DailyPerformanceReport — findings classified CRITICAL/HIGH/MEDIUM/LOW
                            with GO/NO-GO verdict
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

LATENCY_GO_S: float = 2.0
LATENCY_WARN_S: float = 5.0
LATENCY_HIGH_S: float = 8.0

JSONL_PROJECTION_WARN_MB: float = 50.0
CACHE_HIT_WARN_RATIO: float = 0.70
HOT_PATH_MAX_READS: int = 5

SEVERITY_GO = "GO"
SEVERITY_WARN = "WARN"
SEVERITY_HIGH = "HIGH"
SEVERITY_CRITICAL = "CRITICAL"

_SEV_RANK = {SEVERITY_GO: 0, SEVERITY_WARN: 1, SEVERITY_HIGH: 2, SEVERITY_CRITICAL: 3}


def _max_sev(*severities: str) -> str:
    return max(severities, key=lambda s: _SEV_RANK.get(s, 0))


def _classify_latency(latency_s: float) -> str:
    if latency_s < LATENCY_GO_S:
        return SEVERITY_GO
    if latency_s < LATENCY_WARN_S:
        return SEVERITY_WARN
    if latency_s < LATENCY_HIGH_S:
        return SEVERITY_HIGH
    return SEVERITY_CRITICAL


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class FileGrowthRecord:
    path: str
    size_mb: float
    growth_24h_mb: float | None
    projection_30d_mb: float | None
    severity: str

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("size_mb", "growth_24h_mb", "projection_30d_mb"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        return d


@dataclass
class BenchmarkRecord:
    timestamp: str
    fetch_s: float | None
    render_s: float | None
    telegram_s: float | None
    total_s: float
    severity: str
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("fetch_s", "render_s", "telegram_s"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        d["total_s"] = round(d["total_s"], 3)
        return d


@dataclass
class PerformanceFinding:
    severity: str  # CRITICAL / HIGH / MEDIUM / LOW
    category: str  # latency | file_growth | hot_path | benchmark | cache
    message: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyPerformanceReport:
    generated_at: str
    # 1. Latency
    latency_s: float | None
    latency_severity: str
    latency_7d_avg_s: float | None
    latency_trend: str  # "improving" | "stable" | "degrading" | "insufficient_data"
    # 2. File growth
    file_growth: list[dict] = field(default_factory=list)
    # 3. Hot path
    hot_path_read_count: int = 0
    hot_path_severity: str = SEVERITY_GO
    # 4. Benchmark
    benchmark: dict = field(default_factory=dict)
    # 5. Cache
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit_ratio: float | None = None
    cache_severity: str = SEVERITY_GO
    # Findings
    findings: list[dict] = field(default_factory=list)
    verdict: str = SEVERITY_GO

    def to_dict(self) -> dict:
        return asdict(self)

    def to_telegram(self) -> str:
        _verdict_icon = {
            SEVERITY_GO: "🟢", SEVERITY_WARN: "🟡",
            SEVERITY_HIGH: "🔴", SEVERITY_CRITICAL: "🚨",
        }
        _sev_icon = {
            SEVERITY_GO: "✅", SEVERITY_WARN: "⚠️",
            SEVERITY_HIGH: "🔴", SEVERITY_CRITICAL: "🚨",
        }
        vi = _verdict_icon.get(self.verdict, "⚪")
        lines = [
            f"🔬 *Performance Guard — Auto Health*",
            f"{vi} Verdict: *{self.verdict}*\n",
        ]

        # Latency
        si = _sev_icon.get(self.latency_severity, "❓")
        lat = f"{self.latency_s:.1f}s" if self.latency_s is not None else "N/A"
        avg = f"{self.latency_7d_avg_s:.1f}s" if self.latency_7d_avg_s is not None else "—"
        lines.append(f"*Latência executive-summary*")
        lines.append(f"  {si} Atual: `{lat}` | 7d avg: `{avg}` | {self.latency_trend}")

        # File growth
        if self.file_growth:
            lines.append(f"\n*Crescimento JSONL*")
            for fg in self.file_growth[:4]:
                proj = (
                    f"{fg['projection_30d_mb']:.1f}MB/30d"
                    if fg.get("projection_30d_mb") is not None
                    else "—"
                )
                fsi = _sev_icon.get(fg.get("severity", SEVERITY_GO), "✅")
                name = Path(fg["path"]).name
                lines.append(f"  {fsi} `{name}`: {fg['size_mb']:.1f}MB | proj: {proj}")

        # Hot path
        if self.hot_path_read_count > 0:
            hsi = _sev_icon.get(self.hot_path_severity, "✅")
            lines.append(f"\n*Hot Path*")
            lines.append(f"  {hsi} Leituras/request: `{self.hot_path_read_count}`")

        # Benchmark
        if self.benchmark:
            total = self.benchmark.get("total_s", 0)
            bsi = _sev_icon.get(self.benchmark.get("severity", SEVERITY_GO), "✅")
            fetch = self.benchmark.get("fetch_s")
            lines.append(f"\n*Benchmark Digest*")
            fetch_str = f"`{fetch:.1f}s`" if fetch is not None else "—"
            lines.append(f"  {bsi} Total: `{total:.1f}s` | fetch: {fetch_str}")

        # Cache
        if self.cache_hit_ratio is not None:
            csi = _sev_icon.get(self.cache_severity, "✅")
            ratio_pct = f"{self.cache_hit_ratio * 100:.0f}%"
            lines.append(f"\n*Cache HistoryReader*")
            lines.append(
                f"  {csi} Hit ratio: `{ratio_pct}` "
                f"({self.cache_hits}H / {self.cache_misses}M)"
            )

        # Findings (CRITICAL + HIGH only, max 4)
        critical_high = [
            f for f in self.findings
            if f.get("severity") in (SEVERITY_CRITICAL, SEVERITY_HIGH)
        ]
        if critical_high:
            lines.append(f"\n*🚨 Findings*")
            for f in critical_high[:4]:
                lines.append(f"  [{f['severity']}] {f['message']}")

        lines.append("\n_/watchdog performance_")
        return "\n".join(lines)


# ── 1. Performance Guard (latency + daily history) ────────────────────────────

class PerformanceGuard:
    """Measures executive-summary computation latency and stores daily history."""

    def __init__(self, history_path: str | None = None) -> None:
        from core.config import settings
        base = history_path or str(
            Path(settings.auto_healing_history_path).parent
            / "auto_healing_performance.jsonl"
        )
        self._path = Path(base)

    def measure(self, window_hours: int = 24) -> tuple[float | None, str, str | None]:
        """Run generate() and return (latency_s, severity, error)."""
        from app.auto_healing.analytics import HistoryReader
        from app.auto_healing.intelligence import ExecutiveReporter
        try:
            reader = HistoryReader()
            t0 = time.monotonic()
            ExecutiveReporter(history_reader=reader).generate(window_hours=window_hours)
            latency_s = time.monotonic() - t0
            return latency_s, _classify_latency(latency_s), None
        except Exception as exc:
            logger.warning("performance_guard.measure failed: %s", exc, exc_info=True)
            return None, SEVERITY_CRITICAL, str(exc)

    def save(self, record: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.warning("performance_guard.save failed: %s", exc)

    def load_history(self, days: int = 7) -> list[dict]:
        if not self._path.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        entries: list[dict] = []
        try:
            with self._path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        ts_str = e.get("timestamp", "")
                        if ts_str:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if ts >= cutoff:
                                entries.append(e)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError as exc:
            logger.warning("performance_guard.load_history failed: %s", exc)
        return entries

    def compute_7d_stats(self) -> tuple[float | None, str]:
        """Return (avg_latency_s, trend) over last 7 days."""
        history = self.load_history(days=7)
        latencies = [
            e["latency_s"] for e in history
            if e.get("latency_s") is not None
        ]
        if not latencies:
            return None, "insufficient_data"
        avg = sum(latencies) / len(latencies)
        if len(latencies) < 3:
            return round(avg, 2), "insufficient_data"
        # trend: compare first half vs second half
        mid = len(latencies) // 2
        first_avg = sum(latencies[:mid]) / mid
        second_avg = sum(latencies[mid:]) / (len(latencies) - mid)
        delta = second_avg - first_avg
        if delta > 0.5:
            trend = "degrading"
        elif delta < -0.5:
            trend = "improving"
        else:
            trend = "stable"
        return round(avg, 2), trend


# ── 2. File Growth Watchdog ───────────────────────────────────────────────────

class FileGrowthWatchdog:
    """Monitors JSONL file sizes, computes 24h growth, projects 30 days."""

    def __init__(self, history_path: str | None = None) -> None:
        from core.config import settings
        base_dir = Path(history_path or settings.auto_healing_history_path).parent
        self._base_dir = base_dir
        self._sizes_path = base_dir / "auto_healing_file_sizes.jsonl"

    def _save_snapshot(self, snapshots: list[dict]) -> None:
        try:
            self._sizes_path.parent.mkdir(parents=True, exist_ok=True)
            with self._sizes_path.open("a", encoding="utf-8") as fh:
                for s in snapshots:
                    fh.write(json.dumps(s) + "\n")
        except OSError as exc:
            logger.warning("file_growth_watchdog.save_snapshot failed: %s", exc)

    def _load_snapshots(self, hours: int = 48) -> list[dict]:
        if not self._sizes_path.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        entries = []
        try:
            with self._sizes_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        ts = datetime.fromisoformat(
                            e.get("timestamp", "").replace("Z", "+00:00")
                        )
                        if ts >= cutoff:
                            entries.append(e)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError as exc:
            logger.warning("file_growth_watchdog.load_snapshots failed: %s", exc)
        return entries

    def measure(self) -> list[FileGrowthRecord]:
        jsonl_files = sorted(self._base_dir.glob("*.jsonl"))
        if not jsonl_files:
            return []

        now_ts = datetime.now(timezone.utc).isoformat()
        current_sizes: dict[str, float] = {}
        for p in jsonl_files:
            try:
                current_sizes[str(p)] = p.stat().st_size / (1024 * 1024)
            except OSError:
                pass

        # Save current snapshot
        self._save_snapshot([
            {"timestamp": now_ts, "path": path, "size_mb": size}
            for path, size in current_sizes.items()
        ])

        # Load previous snapshots for growth calculation
        past = self._load_snapshots(hours=25)  # slightly over 24h
        past_by_path: dict[str, list[dict]] = {}
        for e in past:
            past_by_path.setdefault(e["path"], []).append(e)

        records = []
        for path, size_mb in current_sizes.items():
            growth_24h_mb = None
            projection_30d_mb = None
            path_history = past_by_path.get(path, [])
            if path_history:
                # Oldest entry in the window
                oldest = min(path_history, key=lambda e: e["timestamp"])
                oldest_size = oldest.get("size_mb", size_mb)
                # Hours elapsed
                try:
                    oldest_ts = datetime.fromisoformat(
                        oldest["timestamp"].replace("Z", "+00:00")
                    )
                    elapsed_h = (
                        datetime.now(timezone.utc) - oldest_ts
                    ).total_seconds() / 3600
                    if elapsed_h > 0:
                        growth_24h_mb = round(size_mb - oldest_size, 3)
                        daily_rate = growth_24h_mb / (elapsed_h / 24)
                        projection_30d_mb = round(size_mb + daily_rate * 30, 2)
                except (ValueError, ZeroDivisionError):
                    pass

            if projection_30d_mb is not None and projection_30d_mb > JSONL_PROJECTION_WARN_MB:
                severity = SEVERITY_CRITICAL if projection_30d_mb > JSONL_PROJECTION_WARN_MB * 2 else SEVERITY_WARN
            else:
                severity = SEVERITY_GO

            records.append(FileGrowthRecord(
                path=path,
                size_mb=round(size_mb, 3),
                growth_24h_mb=growth_24h_mb,
                projection_30d_mb=projection_30d_mb,
                severity=severity,
            ))

        return records


# ── 3. Hot Path Detector ──────────────────────────────────────────────────────

class HotPathDetector:
    """Checks HistoryReader class-level read counters for excessive file access."""

    def snapshot_and_reset(self) -> tuple[int, int, int]:
        """Return (hits, misses, file_reads) and reset counters."""
        from app.auto_healing.analytics import HistoryReader
        hits = HistoryReader._cache_hits
        misses = HistoryReader._cache_misses
        HistoryReader._cache_hits = 0
        HistoryReader._cache_misses = 0
        return hits, misses, misses  # misses == actual file reads

    def check(self, file_reads: int) -> tuple[str, str | None]:
        """Return (severity, message|None)."""
        if file_reads > HOT_PATH_MAX_READS:
            return (
                SEVERITY_HIGH,
                f"Arquivo lido {file_reads}x em 1 request (limite: {HOT_PATH_MAX_READS})",
            )
        return SEVERITY_GO, None


# ── 4. Synthetic Digest Benchmark ────────────────────────────────────────────

class DigestBenchmark:
    """Times the full executive-summary pipeline: fetch → dict → telegram."""

    def run(self, window_hours: int = 24) -> BenchmarkRecord:
        now = datetime.now(timezone.utc).isoformat()
        t_start = time.monotonic()
        fetch_s = render_s = telegram_s = None
        error = None

        try:
            from app.auto_healing.analytics import HistoryReader
            from app.auto_healing.intelligence import ExecutiveReporter

            # Reset counters before benchmark
            HistoryReader._cache_hits = 0
            HistoryReader._cache_misses = 0

            # fetch
            reader = HistoryReader()
            t0 = time.monotonic()
            report = ExecutiveReporter(history_reader=reader).generate(
                window_hours=window_hours
            )
            fetch_s = round(time.monotonic() - t0, 3)

            # render
            t1 = time.monotonic()
            report.to_dict()
            render_s = round(time.monotonic() - t1, 3)

            # telegram
            t2 = time.monotonic()
            report.to_telegram(mode="daily")
            telegram_s = round(time.monotonic() - t2, 3)

        except Exception as exc:
            logger.warning("digest_benchmark.run failed: %s", exc, exc_info=True)
            error = str(exc)

        total_s = round(time.monotonic() - t_start, 3)
        severity = SEVERITY_CRITICAL if error else _classify_latency(fetch_s if fetch_s is not None else total_s)

        return BenchmarkRecord(
            timestamp=now,
            fetch_s=fetch_s,
            render_s=render_s,
            telegram_s=telegram_s,
            total_s=total_s,
            severity=severity,
            error=error,
        )


# ── 5. Cache Health Monitor ───────────────────────────────────────────────────

class CacheHealthMonitor:
    """Reads class-level cache counters from HistoryReader."""

    def collect(self) -> tuple[int, int, float | None, str]:
        """Return (hits, misses, hit_ratio, severity)."""
        from app.auto_healing.analytics import HistoryReader
        hits = HistoryReader._cache_hits
        misses = HistoryReader._cache_misses
        total = hits + misses
        if total == 0:
            return hits, misses, None, SEVERITY_GO
        ratio = hits / total
        severity = SEVERITY_GO if ratio >= CACHE_HIT_WARN_RATIO else SEVERITY_WARN
        return hits, misses, round(ratio, 3), severity


# ── Daily Report Assembler ────────────────────────────────────────────────────

class PerformanceReporter:
    """Assembles the full DailyPerformanceReport from all monitors."""

    def __init__(self, history_path: str | None = None) -> None:
        self._guard = PerformanceGuard(history_path)
        self._growth = FileGrowthWatchdog(history_path)
        self._hot = HotPathDetector()
        self._bench = DigestBenchmark()
        self._cache = CacheHealthMonitor()

    def run(self, window_hours: int = 24) -> DailyPerformanceReport:
        now = datetime.now(timezone.utc).isoformat()
        findings: list[PerformanceFinding] = []

        # ── 1. Reset class counters before measurements ────────────────────────
        from app.auto_healing.analytics import HistoryReader
        HistoryReader._cache_hits = 0
        HistoryReader._cache_misses = 0

        # ── 2. Latency ─────────────────────────────────────────────────────────
        latency_s, latency_sev, lat_err = self._guard.measure(window_hours)
        latency_7d_avg, latency_trend = self._guard.compute_7d_stats()

        self._guard.save({
            "timestamp": now,
            "latency_s": latency_s,
            "severity": latency_sev,
            "error": lat_err,
        })

        if latency_sev in (SEVERITY_HIGH, SEVERITY_CRITICAL):
            findings.append(PerformanceFinding(
                severity=latency_sev,
                category="latency",
                message=(
                    f"executive-summary demorou {latency_s:.1f}s"
                    if latency_s else "executive-summary falhou"
                ),
                detail={"latency_s": latency_s, "threshold_high_s": LATENCY_HIGH_S},
            ))
        if latency_trend == "degrading":
            findings.append(PerformanceFinding(
                severity=SEVERITY_WARN,
                category="latency",
                message=f"Tendência de degradação: 7d avg={latency_7d_avg}s",
                detail={"avg_7d_s": latency_7d_avg, "trend": latency_trend},
            ))

        # ── 3. Hot path (counters from latency measurement) ────────────────────
        hits_lat, misses_lat, reads_lat = self._hot.snapshot_and_reset()
        hot_sev, hot_msg = self._hot.check(reads_lat)
        if hot_msg:
            findings.append(PerformanceFinding(
                severity=hot_sev, category="hot_path", message=hot_msg,
                detail={"reads": reads_lat, "max_allowed": HOT_PATH_MAX_READS},
            ))

        # ── 4. File growth ─────────────────────────────────────────────────────
        growth_records = self._growth.measure()
        for gr in growth_records:
            if gr.severity != SEVERITY_GO:
                findings.append(PerformanceFinding(
                    severity=gr.severity,
                    category="file_growth",
                    message=(
                        f"{Path(gr.path).name}: projeção 30d = "
                        f"{gr.projection_30d_mb:.1f}MB (limite: {JSONL_PROJECTION_WARN_MB}MB)"
                    ),
                    detail=gr.to_dict(),
                ))

        # ── 5. Benchmark (reset counters first) ────────────────────────────────
        HistoryReader._cache_hits = 0
        HistoryReader._cache_misses = 0
        bench = self._bench.run(window_hours)

        if bench.severity in (SEVERITY_HIGH, SEVERITY_CRITICAL):
            findings.append(PerformanceFinding(
                severity=bench.severity,
                category="benchmark",
                message=f"Benchmark digest: total={bench.total_s:.1f}s",
                detail=bench.to_dict(),
            ))
        if bench.error:
            findings.append(PerformanceFinding(
                severity=SEVERITY_CRITICAL,
                category="benchmark",
                message=f"Benchmark falhou: {bench.error[:80]}",
            ))

        # ── 6. Cache health (counters from benchmark) ──────────────────────────
        c_hits, c_misses, c_ratio, c_sev = self._cache.collect()
        if c_ratio is not None and c_sev != SEVERITY_GO:
            findings.append(PerformanceFinding(
                severity=c_sev,
                category="cache",
                message=(
                    f"Cache hit ratio {c_ratio * 100:.0f}% "
                    f"abaixo do mínimo {CACHE_HIT_WARN_RATIO * 100:.0f}%"
                ),
                detail={"hits": c_hits, "misses": c_misses, "ratio": c_ratio},
            ))

        # ── 7. Verdict ─────────────────────────────────────────────────────────
        verdict = SEVERITY_GO
        for f in findings:
            verdict = _max_sev(verdict, f.severity)

        return DailyPerformanceReport(
            generated_at=now,
            latency_s=latency_s,
            latency_severity=latency_sev,
            latency_7d_avg_s=latency_7d_avg,
            latency_trend=latency_trend,
            file_growth=[gr.to_dict() for gr in growth_records],
            hot_path_read_count=reads_lat,
            hot_path_severity=hot_sev,
            benchmark=bench.to_dict(),
            cache_hits=c_hits,
            cache_misses=c_misses,
            cache_hit_ratio=c_ratio,
            cache_severity=c_sev,
            findings=[f.to_dict() for f in findings],
            verdict=verdict,
        )

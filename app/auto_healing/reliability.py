"""Phase 4: Reliability Scoring, Trend Detection, Forecasting, Anomaly Detection.

Components:
- ReliabilityScorer  — 0-100 score per service, computed from history + counters
- SystemSnapshot     — collects disk/memory/queue metrics from the host
- TrendDetector      — detects upward/downward trends in time-series stored in Redis
- Forecaster         — linear extrapolation to estimate when thresholds will be crossed
- AnomalyDetector    — z-score based spike detection over rolling windows

Time-series keys in Redis DB2 (sorted set: member=uuid, score=timestamp):
  auto_heal:ts:disk_used_pct           → disk usage % snapshots
  auto_heal:ts:memory_used_pct         → memory usage % snapshots
  auto_heal:ts:queue_backlog           → normalization backlog count
  auto_heal:ts:restart_count:{service} → restart event ticks (value always 1, count by window)
  auto_heal:ts:heal_latency:{service}  → heal + verify latency in seconds

All sorted sets use a composite member "timestamp:value" to allow
efficient range queries and value extraction without a secondary lookup.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_TS_PREFIX = "auto_heal:ts:"
_TS_MAX_POINTS = 200          # keep last N data points per series
_ANOMALY_MIN_POINTS = 8       # minimum data points for z-score computation
_ANOMALY_Z_THRESHOLD = 2.5    # z-score threshold for anomaly flag
_FORECAST_MIN_POINTS = 4      # minimum points for linear regression


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ServiceScore:
    service: str
    score: float                          # 0.0 – 100.0
    grade: str                            # A+, A, B, C, D, F
    uptime_pct: float | None = None    # fraction of runs where ok=True
    heal_success_rate: float | None = None
    incident_count: int = 0
    recovery_count: int = 0
    last_incident_at: str | None = None
    factors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("score", "uptime_pct", "heal_success_rate"):
            if d[k] is not None:
                d[k] = round(d[k], 2)
        return d


@dataclass
class TrendPoint:
    timestamp: float   # unix epoch
    value: float


@dataclass
class TrendResult:
    series: str
    service: str | None
    slope_per_hour: float              # positive = rising, negative = falling
    direction: str                     # "rising" | "falling" | "stable"
    current_value: float | None
    data_points: int
    window_hours: float


@dataclass
class ForecastResult:
    computed_at: str
    disk: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    queue_backlog: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Anomaly:
    anomaly_type: str        # "restart_spike" | "memory_spike" | "queue_spike" | "latency_spike"
    service: str | None
    detected_at: str
    current_value: float
    baseline_mean: float
    baseline_stddev: float
    z_score: float
    severity: str            # "HIGH" | "MEDIUM" | "LOW"
    description: str

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("current_value", "baseline_mean", "baseline_stddev", "z_score"):
            d[k] = round(d[k], 3)
        return d


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis():
    import redis as redis_lib

    from core.config import settings
    return redis_lib.from_url(settings.redis_url, socket_connect_timeout=2, decode_responses=True)


def _ts_key(series: str, service: str | None = None) -> str:
    if service:
        return f"{_TS_PREFIX}{series}:{service}"
    return f"{_TS_PREFIX}{series}"


def _store_point(key: str, value: float, client=None) -> None:
    """Store a (timestamp, value) point in a Redis sorted set."""
    try:
        c = client or _redis()
        now = datetime.now(timezone.utc).timestamp()
        member = f"{now:.3f}:{value:.4f}"
        c.zadd(key, {member: now})
        count = c.zcard(key)
        if count > _TS_MAX_POINTS:
            c.zpopmin(key, count - _TS_MAX_POINTS)
    except Exception as exc:
        logger.debug("reliability: _store_point failed for %s: %s", key, exc)


def _read_points(key: str, max_age_hours: float = 72.0, client=None) -> list[TrendPoint]:
    """Read time series points from Redis sorted set, within max_age_hours."""
    try:
        c = client or _redis()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).timestamp()
        raw = c.zrangebyscore(key, cutoff, "+inf", withscores=True)
        points: list[TrendPoint] = []
        for member, score in raw:
            try:
                # member format: "{timestamp}:{value}"
                _, val_str = member.rsplit(":", 1)
                points.append(TrendPoint(timestamp=score, value=float(val_str)))
            except (ValueError, AttributeError):
                continue
        return points
    except Exception as exc:
        logger.debug("reliability: _read_points failed for %s: %s", key, exc)
        return []


# ── SystemSnapshot ────────────────────────────────────────────────────────────

class SystemSnapshot:
    """Collects and persists system metrics for trending.

    Call snapshot() from watchdog.py after each run to feed the time-series.
    Designed to run inside the scheduler container (Linux, /proc available).
    """

    def snapshot(self, health_list: list[dict] | None = None) -> dict:
        """Collect current system metrics and store in Redis time series."""
        data: dict = {}
        try:
            client = _redis()
            data.update(self._disk(client))
            data.update(self._memory(client))
            if health_list:
                data.update(self._queue_backlog(health_list, client))
        except Exception as exc:
            logger.debug("reliability: snapshot error: %s", exc)
        return data

    def _disk(self, client) -> dict:
        try:
            import shutil
            usage = shutil.disk_usage("/")
            pct = usage.used / usage.total * 100
            _store_point(_ts_key("disk_used_pct"), pct, client)
            return {"disk_used_pct": round(pct, 2), "disk_free_gb": round(usage.free / 1e9, 2)}
        except Exception as exc:
            logger.debug("reliability: disk snapshot failed: %s", exc)
            return {}

    def _memory(self, client) -> dict:
        try:
            mem = _parse_meminfo()
            if mem:
                pct = (mem["total"] - mem["available"]) / mem["total"] * 100
                _store_point(_ts_key("memory_used_pct"), pct, client)
                return {
                    "memory_used_pct": round(pct, 2),
                    "memory_free_mb": round(mem["available"] / 1024, 1),
                }
        except Exception as exc:
            logger.debug("reliability: memory snapshot failed: %s", exc)
        return {}

    def _queue_backlog(self, health_list: list[dict], client) -> dict:
        try:
            for h in health_list:
                if h.get("name") in ("queues", "bullmq"):
                    backlog = (h.get("evidence") or {}).get("failed_normalization", 0) or 0
                    _store_point(_ts_key("queue_backlog"), float(backlog), client)
                    return {"queue_backlog": int(backlog)}
        except Exception as exc:
            logger.debug("reliability: queue backlog snapshot failed: %s", exc)
        return {}


# ── ReliabilityScorer ─────────────────────────────────────────────────────────

class ReliabilityScorer:
    """Computes a 0-100 reliability score per service.

    Scoring model:
      Base = 100
      - Uptime deduction:      (1 - uptime_pct) * 40   → max -40
      - Incident deduction:    min(incidents, 10) * 2  → max -20
      - Failed heal deduction: heals_failed * 3        → max -15
      - Circuit open deduction: circuit_opens * 5      → max -10
      - Cooldown block bonus:  +5 if cooldown working (≥1 cooldown_block ever)
      Score is clamped to [0, 100].

    Grade: A+ ≥ 98, A ≥ 90, B ≥ 75, C ≥ 60, D ≥ 45, F < 45
    """

    def __init__(self, window_hours: int = 168, history_reader=None) -> None:
        self._window_hours = window_hours
        self._reader = history_reader

    def score_all(self) -> dict[str, ServiceScore]:
        from app.auto_healing.analytics import HistoryReader, MetricsCollector, _status_ok
        reader = self._reader or HistoryReader()
        collector = MetricsCollector()
        entries = reader.read_entries(self._window_hours)

        # Compute per-service uptime from history
        uptime_runs: dict[str, dict] = {}
        for entry in entries:
            for h in entry.get("service_health", []):
                svc = h.get("name", "")
                if svc not in uptime_runs:
                    uptime_runs[svc] = {"ok": 0, "total": 0}
                uptime_runs[svc]["total"] += 1
                if _status_ok(h.get("status", "")):
                    uptime_runs[svc]["ok"] += 1

        # Get incident list for last_incident_at
        incidents = reader.extract_incidents(self._window_hours)
        last_incident: dict[str, str] = {}
        for inc in incidents:
            if inc.service not in last_incident:
                last_incident[inc.service] = inc.detected_at

        scores: dict[str, ServiceScore] = {}

        # Score services that appear in history
        for svc, counts in uptime_runs.items():
            m = collector.read_service_counters(svc)
            uptime = counts["ok"] / counts["total"] if counts["total"] > 0 else 1.0
            score = self._compute_score(uptime, m)
            scores[svc] = ServiceScore(
                service=svc,
                score=score,
                grade=_grade(score),
                uptime_pct=round(uptime * 100, 2),
                heal_success_rate=m.heal_success_rate,
                incident_count=m.incidents_total,
                recovery_count=m.recoveries,
                last_incident_at=last_incident.get(svc),
                factors={
                    "uptime_pct": round(uptime * 100, 2),
                    "heals_attempted": m.heals_attempted,
                    "heals_failed": m.heals_failed,
                    "circuit_opens": m.circuit_opens,
                    "cooldown_blocks": m.cooldown_blocks,
                },
            )

        # Always include critical infra services even if no history yet
        for svc in ("api", "scheduler", "worker", "redis", "postgres"):
            alias = "workers" if svc == "worker" else svc
            if alias not in scores and svc not in scores:
                scores[svc] = ServiceScore(
                    service=svc, score=100.0, grade="A+",
                    uptime_pct=100.0, factors={"note": "no incidents recorded"},
                )

        return scores

    @staticmethod
    def _compute_score(uptime_pct: float, m) -> float:
        score = 100.0
        score -= (1.0 - uptime_pct) * 40
        score -= min(m.incidents_total, 10) * 2
        score -= min(m.heals_failed, 5) * 3
        score -= min(m.circuit_opens, 2) * 5
        if m.cooldown_blocks > 0:
            score += 1.0   # safety mechanism is working
        return round(max(0.0, min(100.0, score)), 2)


# ── TrendDetector ─────────────────────────────────────────────────────────────

class TrendDetector:
    """Detects significant trends in time-series metrics."""

    def detect_all(self, window_hours: float = 24.0) -> list[TrendResult]:
        results: list[TrendResult] = []
        series_specs = [
            ("disk_used_pct", None),
            ("memory_used_pct", None),
            ("queue_backlog", None),
        ]
        for series, service in series_specs:
            key = _ts_key(series, service)
            points = _read_points(key, max_age_hours=window_hours)
            if len(points) >= 2:
                results.append(self._analyze(series, service, points, window_hours))
        return results

    @staticmethod
    def _analyze(series: str, service: str | None, points: list[TrendPoint],
                 window_hours: float) -> TrendResult:
        if len(points) < 2:
            return TrendResult(series=series, service=service, slope_per_hour=0.0,
                               direction="stable", current_value=None, data_points=0,
                               window_hours=window_hours)
        # Simple linear regression on timestamps (in hours from first point)
        t0 = points[0].timestamp
        xs = [(p.timestamp - t0) / 3600.0 for p in points]
        ys = [p.value for p in points]
        slope, _ = _linear_regression(xs, ys)
        current = ys[-1]
        direction = "stable"
        if slope > 0.1:
            direction = "rising"
        elif slope < -0.1:
            direction = "falling"
        return TrendResult(
            series=series, service=service,
            slope_per_hour=round(slope, 4),
            direction=direction,
            current_value=round(current, 2),
            data_points=len(points),
            window_hours=window_hours,
        )


# ── Forecaster ────────────────────────────────────────────────────────────────

class Forecaster:
    """Linear extrapolation to estimate when thresholds will be crossed."""

    DISK_CRITICAL_PCT = 95.0
    QUEUE_CRITICAL = 500
    MEMORY_CRITICAL_PCT = 90.0

    def forecast(self, window_hours: float = 48.0) -> ForecastResult:
        now_str = datetime.now(timezone.utc).isoformat()
        result = ForecastResult(computed_at=now_str)
        result.disk = self._forecast_series("disk_used_pct", self.DISK_CRITICAL_PCT, window_hours)
        result.memory = self._forecast_series("memory_used_pct", self.MEMORY_CRITICAL_PCT, window_hours)
        result.queue_backlog = self._forecast_series(
            "queue_backlog", float(self.QUEUE_CRITICAL), window_hours
        )
        return result

    @staticmethod
    def _forecast_series(series: str, threshold: float, window_hours: float) -> dict:
        key = _ts_key(series)
        points = _read_points(key, max_age_hours=window_hours)
        current = points[-1].value if points else None

        if len(points) < _FORECAST_MIN_POINTS or current is None:
            return {
                "current": current,
                "data_points": len(points),
                "trend_per_hour": None,
                "eta_threshold_hours": None,
                "eta_threshold_at": None,
                "threshold": threshold,
                "status": "insufficient_data",
            }

        t0 = points[0].timestamp
        xs = [(p.timestamp - t0) / 3600.0 for p in points]
        ys = [p.value for p in points]
        slope, intercept = _linear_regression(xs, ys)

        eta_hours: float | None = None
        eta_at: str | None = None
        if slope > 0.001 and current < threshold:
            # hours until threshold: (threshold - current) / slope
            hours_remaining = (threshold - current) / slope
            if hours_remaining > 0:
                eta_hours = round(hours_remaining, 1)
                eta_dt = datetime.now(timezone.utc) + timedelta(hours=hours_remaining)
                eta_at = eta_dt.isoformat()

        status = "stable"
        if slope > 0.1:
            status = "rising"
        if eta_hours is not None and eta_hours < 24:
            status = "critical"
        elif eta_hours is not None and eta_hours < 72:
            status = "warning"

        return {
            "current": round(current, 2) if current is not None else None,
            "data_points": len(points),
            "trend_per_hour": round(slope, 4),
            "eta_threshold_hours": eta_hours,
            "eta_threshold_at": eta_at,
            "threshold": threshold,
            "status": status,
        }


# ── AnomalyDetector ───────────────────────────────────────────────────────────

class AnomalyDetector:
    """Z-score based anomaly detection over rolling windows."""

    def detect_all(self, window_hours: float = 24.0) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        now = datetime.now(timezone.utc).isoformat()

        # Check disk
        anomalies.extend(self._check_series("disk_used_pct", "disk_spike", None,
                                             window_hours, now, high_only=True))
        # Check memory
        anomalies.extend(self._check_series("memory_used_pct", "memory_spike", None,
                                             window_hours, now, high_only=True))
        # Check queue
        anomalies.extend(self._check_series("queue_backlog", "queue_spike", None,
                                             window_hours, now, high_only=True))

        # Check restart counts per service (from analytics counters)
        for svc in ("redis", "workers", "scheduler"):
            anomalies.extend(self._check_restart_spike(svc, window_hours, now))

        return sorted(anomalies, key=lambda a: a.z_score, reverse=True)

    @staticmethod
    def _check_series(series: str, anomaly_type: str, service: str | None,
                      window_hours: float, now: str, high_only: bool = False) -> list[Anomaly]:
        key = _ts_key(series, service)
        points = _read_points(key, max_age_hours=window_hours)
        if len(points) < _ANOMALY_MIN_POINTS:
            return []

        values = [p.value for p in points]
        # Use all-but-last as baseline, last as current
        baseline = values[:-1]
        current = values[-1]
        mean = sum(baseline) / len(baseline)
        std = math.sqrt(sum((v - mean) ** 2 for v in baseline) / len(baseline))
        if std < 0.001:
            return []
        z = (current - mean) / std
        if high_only and z <= 0:
            return []
        if abs(z) < _ANOMALY_Z_THRESHOLD:
            return []

        severity = "HIGH" if abs(z) >= 4.0 else "MEDIUM" if abs(z) >= 3.0 else "LOW"
        return [Anomaly(
            anomaly_type=anomaly_type,
            service=service,
            detected_at=now,
            current_value=current,
            baseline_mean=mean,
            baseline_stddev=std,
            z_score=z,
            severity=severity,
            description=(
                f"{series} is {current:.2f} vs baseline {mean:.2f}±{std:.2f} "
                f"(z={z:.2f})"
            ),
        )]

    @staticmethod
    def _check_restart_spike(service: str, window_hours: float, now: str) -> list[Anomaly]:
        """Detect restart spikes by counting heal attempts in time windows."""
        try:
            from app.auto_healing.analytics import HistoryReader
            reader = HistoryReader()
            entries = reader.read_entries(int(window_hours))
            # Bucket heal attempts into 1-hour windows
            from collections import defaultdict
            buckets: dict[int, int] = defaultdict(int)
            for entry in entries:
                ts = entry.get("timestamp", "")
                from app.auto_healing.analytics import _parse_ts
                t = _parse_ts(ts)
                if not t:
                    continue
                hour_bucket = int(t.timestamp() // 3600)
                for hr in entry.get("heal_results", []):
                    if hr.get("service") == service:
                        buckets[hour_bucket] += 1
            if len(buckets) < _ANOMALY_MIN_POINTS:
                return []
            counts = sorted(buckets.values())
            current = counts[-1]
            baseline = counts[:-1]
            mean = sum(baseline) / len(baseline)
            std = math.sqrt(sum((v - mean) ** 2 for v in baseline) / max(len(baseline), 1))
            if std < 0.001 or current == 0:
                return []
            z = (current - mean) / std
            if z < _ANOMALY_Z_THRESHOLD:
                return []
            severity = "HIGH" if z >= 4.0 else "MEDIUM" if z >= 3.0 else "LOW"
            return [Anomaly(
                anomaly_type="restart_spike",
                service=service,
                detected_at=now,
                current_value=float(current),
                baseline_mean=mean,
                baseline_stddev=std,
                z_score=z,
                severity=severity,
                description=(
                    f"{service} had {current} heal attempts in the last hour "
                    f"vs baseline {mean:.2f}±{std:.2f}"
                ),
            )]
        except Exception as exc:
            logger.debug("reliability: restart spike check failed for %s: %s", service, exc)
            return []


# ── Math helpers ──────────────────────────────────────────────────────────────

def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares: returns (slope, intercept)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=False))
    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-10:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _parse_meminfo() -> dict[str, int] | None:
    """Parse /proc/meminfo (Linux only). Returns kB values."""
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        info: dict[str, int] = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                val = int(parts[1])
                info[key] = val
        return {
            "total": info.get("MemTotal", 0),
            "free": info.get("MemFree", 0),
            "available": info.get("MemAvailable", info.get("MemFree", 0)),
            "buffers": info.get("Buffers", 0),
            "cached": info.get("Cached", 0),
        }
    except (OSError, ValueError):
        return None


def _grade(score: float) -> str:
    if score >= 98:
        return "A+"
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 45:
        return "D"
    return "F"

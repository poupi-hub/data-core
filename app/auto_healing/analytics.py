"""Phase 3: Operational Analytics for AutoHealingWatchdog.

Responsibilities:
- MetricsCollector  — records counters to Redis DB2 after every watchdog run
- HistoryReader     — parses auto_healing_watchdog.jsonl to extract incidents + MTTR
- DailyReporter     — generates daily summary from history
- ServiceMetrics    — aggregated per-service view
- GlobalMetrics     — cross-service totals + rates
- IncidentRecord    — a single detected + resolved (or open) incident

Counter keys in Redis DB2 (cumulative; never reset):
  auto_heal:metrics:incidents:{service}
  auto_heal:metrics:heals_attempted:{service}
  auto_heal:metrics:heals_successful:{service}
  auto_heal:metrics:heals_failed:{service}
  auto_heal:metrics:cooldown_blocks:{service}
  auto_heal:metrics:circuit_opens:{service}
  auto_heal:metrics:recoveries:{service}
  auto_heal:metrics:recovery_seconds:{service}   ← sum of all recovery durations (int)
  auto_heal:metrics:recovery_durations:{service} ← sorted set: member=uuid, score=duration_s
                                                    used for p95 computation
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_M = "auto_heal:metrics:"
_DURATIONS_KEY = _M + "recovery_durations:{service}"
_DURATIONS_MAX = 500  # keep at most 500 data points per service for p95


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ServiceMetrics:
    service: str
    incidents_total: int = 0
    heals_attempted: int = 0
    heals_successful: int = 0
    heals_failed: int = 0
    cooldown_blocks: int = 0
    circuit_opens: int = 0
    recoveries: int = 0
    mttr_avg_seconds: float | None = None
    mttr_p95_seconds: float | None = None
    heal_success_rate: float | None = None  # heals_successful / heals_attempted
    recovery_rate: float | None = None      # recoveries / incidents_total

    def to_dict(self) -> dict:
        d = asdict(self)
        # Round floats for readability
        for k in ("mttr_avg_seconds", "mttr_p95_seconds",
                  "heal_success_rate", "recovery_rate"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        return d


@dataclass
class GlobalMetrics:
    window_hours: int = 168
    generated_at: str = ""
    services: dict[str, ServiceMetrics] = field(default_factory=dict)
    incidents_total: int = 0
    heals_attempted: int = 0
    heals_successful: int = 0
    heals_failed: int = 0
    cooldown_blocks: int = 0
    circuit_opens: int = 0
    recoveries: int = 0
    heal_success_rate: float | None = None
    recovery_rate: float | None = None
    mttr_avg_seconds: float | None = None

    def to_dict(self) -> dict:
        d = {
            "window_hours": self.window_hours,
            "generated_at": self.generated_at,
            "global": {
                "incidents_total": self.incidents_total,
                "heals_attempted": self.heals_attempted,
                "heals_successful": self.heals_successful,
                "heals_failed": self.heals_failed,
                "cooldown_blocks": self.cooldown_blocks,
                "circuit_opens": self.circuit_opens,
                "recoveries": self.recoveries,
                "heal_success_rate": (
                    round(self.heal_success_rate, 3) if self.heal_success_rate is not None else None
                ),
                "recovery_rate": (
                    round(self.recovery_rate, 3) if self.recovery_rate is not None else None
                ),
                "mttr_avg_seconds": (
                    round(self.mttr_avg_seconds, 1) if self.mttr_avg_seconds is not None else None
                ),
            },
            "by_service": {k: v.to_dict() for k, v in self.services.items()},
        }
        return d


@dataclass
class IncidentRecord:
    service: str
    detected_at: str           # ISO timestamp
    resolved_at: str | None = None
    outcome: str = "open"      # "recovered" | "unresolved" | "open"
    heal_attempts: int = 0
    heal_outcomes: list[str] = field(default_factory=list)
    duration_seconds: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyReport:
    report_date: str
    incidents: int = 0
    heals_attempted: int = 0
    heals_successful: int = 0
    heals_failed: int = 0
    cooldown_blocks: int = 0
    circuit_opens: int = 0
    recoveries: int = 0
    heal_success_rate: float | None = None
    recovery_rate: float | None = None
    mttr_avg_seconds: float | None = None
    by_service: dict[str, dict] = field(default_factory=dict)
    incidents_detail: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("heal_success_rate", "recovery_rate", "mttr_avg_seconds"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        return d

    def to_text(self) -> str:
        """Human-readable daily report (for Telegram or logs)."""
        lines = [
            f"📊 AutoHealing — Relatório Diário {self.report_date}",
            "",
            f"🔴 Incidentes:        {self.incidents}",
            f"🔧 Heals tentados:    {self.heals_attempted}",
            f"✅ Heals OK:          {self.heals_successful}",
            f"❌ Heals falhou:      {self.heals_failed}",
            f"⏸  Cooldown blocks:   {self.cooldown_blocks}",
            f"🔒 Circuit opens:     {self.circuit_opens}",
            f"🟢 Recoveries:        {self.recoveries}",
        ]
        if self.heal_success_rate is not None:
            lines.append(f"📈 Taxa sucesso heal: {self.heal_success_rate * 100:.1f}%")
        if self.mttr_avg_seconds is not None:
            lines.append(f"⏱  MTTR médio:        {self.mttr_avg_seconds:.0f}s")
        if self.by_service:
            lines.append("")
            lines.append("Por serviço:")
            for svc, stats in sorted(self.by_service.items()):
                if stats.get("incidents_total", 0) > 0 or stats.get("heals_attempted", 0) > 0:
                    lines.append(
                        f"  • {svc}: {stats.get('incidents_total', 0)} incidentes, "
                        f"{stats.get('heals_successful', 0)}/{stats.get('heals_attempted', 0)} heals OK"
                    )
        return "\n".join(lines)


# ── Redis client (reuses cooldown pattern) ────────────────────────────────────

def _redis():
    import redis as redis_lib

    from core.config import settings
    return redis_lib.from_url(settings.redis_url, socket_connect_timeout=2, decode_responses=True)


# ── MetricsCollector ─────────────────────────────────────────────────────────

class MetricsCollector:
    """Increments Redis counters after each WatchdogExecution.

    Call record_run(execution) from watchdog.py after each cycle.
    Fails silently — never raises. Analytics must not break healing.
    """

    def record_run(self, execution_dict: dict) -> None:
        """Parse a WatchdogExecution.to_dict() and update Redis counters."""
        try:
            self._process(execution_dict)
        except Exception as exc:
            logger.warning("analytics: record_run failed: %s", exc)

    def _process(self, d: dict) -> None:
        from app.auto_healing.models import HealOutcome
        client = _redis()

        heal_results = d.get("heal_results", [])
        service_health = d.get("service_health", [])

        # ── Process heal outcomes ──
        for hr in heal_results:
            svc = hr.get("service", "unknown")
            outcome = hr.get("outcome", "")
            client.incr(f"{_M}heals_attempted:{svc}")
            if outcome == HealOutcome.RECOVERED:
                client.incr(f"{_M}heals_successful:{svc}")
                client.incr(f"{_M}recoveries:{svc}")
            elif outcome == HealOutcome.FAILED:
                client.incr(f"{_M}heals_failed:{svc}")
            elif outcome == HealOutcome.SKIPPED:
                client.incr(f"{_M}cooldown_blocks:{svc}")
            elif outcome == HealOutcome.BLOCKED_CIRCUIT:
                client.incr(f"{_M}circuit_opens:{svc}")

        # ── Count unhealthy services as incident ticks ──
        # An "incident" counter is per-run degradation signal; true incidents
        # (with start/end) come from HistoryReader.
        for h in service_health:
            if not _status_ok(h.get("status", "")):
                svc = h.get("name", "unknown")
                client.incr(f"{_M}incidents:{svc}")

    def record_recovery_duration(self, service: str, duration_seconds: float) -> None:
        """Record a recovery duration for MTTR p95 calculation."""
        try:
            client = _redis()
            key = _DURATIONS_KEY.format(service=service)
            member = uuid.uuid4().hex
            client.zadd(key, {member: duration_seconds})
            # Keep sorted set bounded
            count = client.zcard(key)
            if count > _DURATIONS_MAX:
                client.zpopmin(key, count - _DURATIONS_MAX)
            # Update sum + count for fast avg
            client.incrbyfloat(f"{_M}recovery_seconds:{service}", duration_seconds)
            client.incr(f"{_M}recovery_count:{service}")
        except Exception as exc:
            logger.warning("analytics: record_recovery_duration failed: %s", exc)

    def read_service_counters(self, service: str) -> ServiceMetrics:
        """Read all counters for a single service from Redis."""
        try:
            client = _redis()
            keys = [
                f"{_M}incidents:{service}",
                f"{_M}heals_attempted:{service}",
                f"{_M}heals_successful:{service}",
                f"{_M}heals_failed:{service}",
                f"{_M}cooldown_blocks:{service}",
                f"{_M}circuit_opens:{service}",
                f"{_M}recoveries:{service}",
                f"{_M}recovery_seconds:{service}",
                f"{_M}recovery_count:{service}",
            ]
            values = client.mget(keys)
            (incidents, attempted, successful, failed,
             cooldowns, circuits, recoveries,
             rec_secs, rec_count) = [int(float(v or 0)) for v in values]

            mttr_avg = (float(client.get(f"{_M}recovery_seconds:{service}") or 0) / rec_count
                        if rec_count > 0 else None)
            mttr_p95 = self._p95(service, client)
            heal_rate = successful / attempted if attempted > 0 else None
            rec_rate = recoveries / incidents if incidents > 0 else None

            return ServiceMetrics(
                service=service,
                incidents_total=incidents,
                heals_attempted=attempted,
                heals_successful=successful,
                heals_failed=failed,
                cooldown_blocks=cooldowns,
                circuit_opens=circuits,
                recoveries=recoveries,
                mttr_avg_seconds=mttr_avg,
                mttr_p95_seconds=mttr_p95,
                heal_success_rate=heal_rate,
                recovery_rate=rec_rate,
            )
        except Exception as exc:
            logger.warning("analytics: read_service_counters failed for %s: %s", service, exc)
            return ServiceMetrics(service=service)

    def _p95(self, service: str, client) -> float | None:
        try:
            key = _DURATIONS_KEY.format(service=service)
            durations = [float(s) for _, s in client.zrange(key, 0, -1, withscores=True)]
            if len(durations) < 5:
                return None
            durations.sort()
            idx = math.ceil(0.95 * len(durations)) - 1
            return durations[max(0, idx)]
        except Exception:
            return None

    def read_global_metrics(self, window_hours: int = 168) -> GlobalMetrics:
        """Aggregate metrics from Redis across all known services."""
        services = ["redis", "workers", "scheduler", "api", "postgres",
                    "bullmq", "queues", "data-core", "poupi-crypto"]
        per_service: dict[str, ServiceMetrics] = {}
        for svc in services:
            m = self.read_service_counters(svc)
            if (m.incidents_total + m.heals_attempted) > 0:
                per_service[svc] = m

        total_attempted = sum(m.heals_attempted for m in per_service.values())
        total_successful = sum(m.heals_successful for m in per_service.values())
        total_failed = sum(m.heals_failed for m in per_service.values())
        total_incidents = sum(m.incidents_total for m in per_service.values())
        total_recoveries = sum(m.recoveries for m in per_service.values())
        total_cooldowns = sum(m.cooldown_blocks for m in per_service.values())
        total_circuits = sum(m.circuit_opens for m in per_service.values())

        all_mttrs = [m.mttr_avg_seconds for m in per_service.values()
                     if m.mttr_avg_seconds is not None]
        global_mttr = sum(all_mttrs) / len(all_mttrs) if all_mttrs else None

        return GlobalMetrics(
            window_hours=window_hours,
            generated_at=datetime.now(timezone.utc).isoformat(),
            services=per_service,
            incidents_total=total_incidents,
            heals_attempted=total_attempted,
            heals_successful=total_successful,
            heals_failed=total_failed,
            cooldown_blocks=total_cooldowns,
            circuit_opens=total_circuits,
            recoveries=total_recoveries,
            heal_success_rate=total_successful / total_attempted if total_attempted > 0 else None,
            recovery_rate=total_recoveries / total_incidents if total_incidents > 0 else None,
            mttr_avg_seconds=global_mttr,
        )


# ── HistoryReader ─────────────────────────────────────────────────────────────

class HistoryReader:
    """Parses auto_healing_watchdog.jsonl to reconstruct incident timeline."""

    # Class-level counters — reset by PerformanceReporter before each benchmark
    _cache_hits: int = 0
    _cache_misses: int = 0

    def __init__(self, history_path: str | None = None) -> None:
        from core.config import settings
        self._path = Path(history_path or settings.auto_healing_history_path)
        self._entries_cache: dict[int, list[dict]] = {}

    def read_entries(self, window_hours: int = 168) -> list[dict]:
        """Return all history entries within the window (newest first in file = chronological)."""
        if window_hours in self._entries_cache:
            HistoryReader._cache_hits += 1
            return self._entries_cache[window_hours]
        HistoryReader._cache_misses += 1
        if not self._path.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        entries: list[dict] = []
        try:
            with self._path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        ts_str = entry.get("timestamp", "")
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else None
                        if ts and ts >= cutoff:
                            entries.append(entry)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError as exc:
            logger.warning("analytics: cannot read history: %s", exc)
        self._entries_cache[window_hours] = entries
        return entries

    def extract_incidents(self, window_hours: int = 168) -> list[IncidentRecord]:
        """Reconstruct incidents from consecutive unhealthy states in history."""
        entries = self.read_entries(window_hours)
        if not entries:
            return []

        # Track per-service open incidents: service → (detected_at, heal_attempts, outcomes)
        open_incidents: dict[str, tuple[str, int, list[str]]] = {}
        closed: list[IncidentRecord] = []

        for entry in entries:
            ts = entry.get("timestamp", "")
            health_list = entry.get("service_health", [])
            heal_results = entry.get("heal_results", [])

            # Index heal results by service
            heal_by_svc: dict[str, list[str]] = {}
            for hr in heal_results:
                svc = hr.get("service", "")
                heal_by_svc.setdefault(svc, []).append(hr.get("outcome", ""))

            for h in health_list:
                svc = h.get("name", "")
                ok = _status_ok(h.get("status", ""))

                if not ok and svc not in open_incidents:
                    # Incident start
                    open_incidents[svc] = (ts, 0, [])

                if svc in open_incidents:
                    detected_at, attempts, outcomes = open_incidents[svc]
                    # Accumulate heals within this incident
                    for out in heal_by_svc.get(svc, []):
                        attempts += 1
                        outcomes.append(out)
                    open_incidents[svc] = (detected_at, attempts, outcomes)

                    if ok and svc in open_incidents:
                        # Recovery
                        detected_ts = _parse_ts(detected_at)
                        resolved_ts = _parse_ts(ts)
                        duration = (resolved_ts - detected_ts).total_seconds() if (detected_ts and resolved_ts) else None
                        closed.append(IncidentRecord(
                            service=svc,
                            detected_at=detected_at,
                            resolved_at=ts,
                            outcome="recovered",
                            heal_attempts=attempts,
                            heal_outcomes=outcomes,
                            duration_seconds=round(duration, 1) if duration is not None else None,
                        ))
                        del open_incidents[svc]

        # Remaining open incidents
        for svc, (detected_at, attempts, outcomes) in open_incidents.items():
            closed.append(IncidentRecord(
                service=svc,
                detected_at=detected_at,
                resolved_at=None,
                outcome="open",
                heal_attempts=attempts,
                heal_outcomes=outcomes,
                duration_seconds=None,
            ))

        return sorted(closed, key=lambda x: x.detected_at, reverse=True)

    def compute_mttr(self, window_hours: int = 168) -> dict[str, dict]:
        """Compute MTTR avg and p95 per service from incident history."""
        incidents = self.extract_incidents(window_hours)
        durations_by_svc: dict[str, list[float]] = {}
        for inc in incidents:
            if inc.duration_seconds is not None and inc.outcome == "recovered":
                durations_by_svc.setdefault(inc.service, []).append(inc.duration_seconds)

        result: dict[str, dict] = {}
        for svc, durs in durations_by_svc.items():
            durs.sort()
            avg = sum(durs) / len(durs)
            idx = math.ceil(0.95 * len(durs)) - 1
            p95 = durs[max(0, idx)] if durs else None
            result[svc] = {
                "count": len(durs),
                "avg_seconds": round(avg, 1),
                "p95_seconds": round(p95, 1) if p95 is not None else None,
                "min_seconds": round(min(durs), 1),
                "max_seconds": round(max(durs), 1),
            }
        return result

    def healer_stats(self, window_hours: int = 168) -> list[dict]:
        """Per-healer attempt/success/failure breakdown from history."""
        entries = self.read_entries(window_hours)
        from collections import defaultdict
        stats: dict[str, dict] = defaultdict(lambda: {"attempts": 0, "recovered": 0, "failed": 0,
                                                        "skipped": 0, "blocked_circuit": 0})
        for entry in entries:
            for hr in entry.get("heal_results", []):
                svc = hr.get("service", "unknown")
                out = hr.get("outcome", "")
                stats[svc]["attempts"] += 1
                if out == "RECOVERED":
                    stats[svc]["recovered"] += 1
                elif out == "FAILED":
                    stats[svc]["failed"] += 1
                elif out == "SKIPPED":
                    stats[svc]["skipped"] += 1
                elif out == "BLOCKED_CIRCUIT":
                    stats[svc]["blocked_circuit"] += 1

        # Healer name mapping
        _healer_names = {
            "redis": "restart_redis",
            "workers": "restart_worker",
            "scheduler": "restart_scheduler",
            "queues": "normalization_backlog_or_bullmq",
            "bullmq": "bullmq_stalled_cleaner",
        }

        result = []
        for svc, s in sorted(stats.items()):
            attempts = s["attempts"]
            recovered = s["recovered"]
            result.append({
                "healer": _healer_names.get(svc, f"healer_{svc}"),
                "target_service": svc,
                "attempts": attempts,
                "recovered": recovered,
                "failed": s["failed"],
                "skipped": s["skipped"],
                "blocked_circuit": s["blocked_circuit"],
                "success_rate": round(recovered / attempts, 3) if attempts > 0 else None,
            })
        return result


# ── DailyReporter ─────────────────────────────────────────────────────────────

class DailyReporter:
    """Generates a DailyReport from history entries for a specific date."""

    def __init__(self) -> None:
        self._reader = HistoryReader()
        self._collector = MetricsCollector()

    def generate(self, report_date: date | None = None) -> DailyReport:
        """Generate daily report for `report_date` (defaults to today UTC)."""
        if report_date is None:
            report_date = datetime.now(timezone.utc).date()

        date_str = report_date.isoformat()
        cutoff_start = datetime.combine(report_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        cutoff_end = cutoff_start + timedelta(days=1)

        # Filter history entries for this specific day
        all_entries = self._reader.read_entries(window_hours=48)
        day_entries = [
            e for e in all_entries
            if _parse_ts(e.get("timestamp", "")) and
               cutoff_start <= _parse_ts(e.get("timestamp", "")) < cutoff_end  # type: ignore[operator]
        ]

        report = DailyReport(report_date=date_str)
        svc_stats: dict[str, dict] = {}

        open_incidents: dict[str, str] = {}  # service → detected_at

        for entry in day_entries:
            health_list = entry.get("service_health", [])
            heal_results = entry.get("heal_results", [])
            ts = entry.get("timestamp", "")

            # Count heal outcomes
            for hr in heal_results:
                svc = hr.get("service", "?")
                out = hr.get("outcome", "")
                report.heals_attempted += 1
                svc_stats.setdefault(svc, {"incidents_total": 0, "heals_attempted": 0,
                                            "heals_successful": 0, "heals_failed": 0,
                                            "cooldown_blocks": 0, "circuit_opens": 0,
                                            "recoveries": 0})
                svc_stats[svc]["heals_attempted"] += 1
                if out == "RECOVERED":
                    report.heals_successful += 1
                    report.recoveries += 1
                    svc_stats[svc]["heals_successful"] += 1
                    svc_stats[svc]["recoveries"] += 1
                elif out == "FAILED":
                    report.heals_failed += 1
                    svc_stats[svc]["heals_failed"] += 1
                elif out == "SKIPPED":
                    report.cooldown_blocks += 1
                    svc_stats[svc]["cooldown_blocks"] += 1
                elif out == "BLOCKED_CIRCUIT":
                    report.circuit_opens += 1
                    svc_stats[svc]["circuit_opens"] += 1

            # Track incidents (unhealthy → healthy transitions)
            for h in health_list:
                svc = h.get("name", "")
                ok = _status_ok(h.get("status", ""))
                if not ok and svc not in open_incidents:
                    open_incidents[svc] = ts
                    report.incidents += 1
                    svc_stats.setdefault(svc, {"incidents_total": 0, "heals_attempted": 0,
                                                "heals_successful": 0, "heals_failed": 0,
                                                "cooldown_blocks": 0, "circuit_opens": 0,
                                                "recoveries": 0})
                    svc_stats[svc]["incidents_total"] += 1
                if ok and svc in open_incidents:
                    del open_incidents[svc]

        # Compute rates
        if report.heals_attempted > 0:
            report.heal_success_rate = report.heals_successful / report.heals_attempted
        if report.incidents > 0:
            report.recovery_rate = report.recoveries / report.incidents

        # MTTR for today from history reader (scoped to today)
        mttr_map = self._reader.compute_mttr(window_hours=48)
        all_mttrs = [v["avg_seconds"] for v in mttr_map.values()]
        if all_mttrs:
            report.mttr_avg_seconds = round(sum(all_mttrs) / len(all_mttrs), 1)

        report.by_service = svc_stats
        return report


# ── Helpers ───────────────────────────────────────────────────────────────────

def _status_ok(status: str) -> bool:
    return status.upper() in {"OK", "READY", "HEALTHY", "ALIVE"}


def _parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None

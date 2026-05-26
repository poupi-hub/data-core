"""
replay_integrity_burnin_validator.py — Phase S S-5

Validates multi-session replay burn-in continuity by auditing JSONL log files for:
  - JSON parse errors / corruption
  - Temporal gaps > 30 min between consecutive records
  - Session continuity (expected vs actual record counts)
  - Completeness ratio vs audit log

Scores: replay_burnin_score, replay_continuity_score, replay_consistency_score

CLI:
  python -m domains.crypto_coin.research.replay_integrity_burnin_validator
  python -m domains.crypto_coin.research.replay_integrity_burnin_validator --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

REPLAY_LOG = Path("data/replay_burnin_log.jsonl")

# Files to audit for replay continuity (ordered by importance)
REPLAY_TARGETS: list[tuple[str, str]] = [
    ("live_readiness",           "data/live_readiness_revalidation_log.jsonl"),
    ("governance",               "data/runtime_governance_log.jsonl"),
    ("guardian",                 "data/live_guardian_log.jsonl"),
    ("stability",                "data/stability_log.jsonl"),
    ("capital_preservation",     "data/capital_preservation_log.jsonl"),
    ("execution_audit",          "data/live_execution_audit_summary.jsonl"),
    ("burnin",                   "data/runtime_burnin_log.jsonl"),
    ("operational_drift",        "data/operational_drift_log.jsonl"),
]

# Gap threshold: consecutive records more than this apart are flagged
GAP_THRESHOLD_MINUTES = 30.0

try:
    from api.burnin_metrics import replay_burnin_score       as _prom_burnin
    from api.burnin_metrics import replay_continuity_score   as _prom_continuity
    from api.burnin_metrics import replay_consistency_score  as _prom_consistency
    _METRICS = True
except ImportError:
    _METRICS = False


@dataclass
class ReplayCheck:
    session_name: str
    filepath: str
    file_exists: bool
    record_count: int
    parse_errors: int
    gap_count: int
    max_gap_minutes: float
    first_record_ts: str | None
    last_record_ts: str | None
    session_span_minutes: float
    completeness_ratio: float   # records / (span_minutes / expected_interval_min)
    issues: list[str]
    status: str   # healthy | degraded | corrupt | missing


@dataclass
class ReplayBurninReport:
    report_id: str
    replay_burnin_score: float
    replay_continuity_score: float
    replay_consistency_score: float
    total_sessions: int
    healthy_sessions: int
    degraded_sessions: int
    corrupt_sessions: int
    missing_sessions: int
    total_records: int
    total_parse_errors: int
    total_gaps: int
    checks: list[ReplayCheck]
    issues_summary: list[str]
    evaluated_at: str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


class ReplayIntegrityBurninValidator:
    """S-5: Replay Integrity & Burn-In Validator."""

    def __init__(self, log: Path = REPLAY_LOG):
        self.log = log

    def validate(self) -> ReplayBurninReport:
        report_id = str(uuid.uuid4())[:10]
        checks: list[ReplayCheck] = []

        for session_name, filepath in REPLAY_TARGETS:
            checks.append(self._check_session(session_name, filepath))

        healthy   = sum(1 for c in checks if c.status == "healthy")
        degraded  = sum(1 for c in checks if c.status == "degraded")
        corrupt   = sum(1 for c in checks if c.status == "corrupt")
        missing   = sum(1 for c in checks if c.status == "missing")
        total     = len(checks)

        total_records      = sum(c.record_count for c in checks)
        total_parse_errors = sum(c.parse_errors for c in checks)
        total_gaps         = sum(c.gap_count for c in checks)

        # replay_burnin_score: weighted health
        weights = {"healthy": 1.0, "degraded": 0.5, "corrupt": 0.1, "missing": 0.0}
        weighted_sum = sum(weights[c.status] for c in checks)
        replay_burnin = (weighted_sum / total * 100) if total else 0.0

        # replay_continuity_score: penalise gaps
        gap_penalty = min(total_gaps * 5.0, 50.0)
        replay_continuity = max(0.0, replay_burnin - gap_penalty)

        # replay_consistency_score: penalise parse errors
        total_lines = max(total_records + total_parse_errors, 1)
        parse_ratio = total_parse_errors / total_lines
        replay_consistency = max(0.0, 100.0 * (1.0 - parse_ratio * 10))

        issues: list[str] = []
        missing_names = [c.session_name for c in checks if c.status == "missing"]
        corrupt_names = [c.session_name for c in checks if c.status == "corrupt"]
        gap_sessions  = [c.session_name for c in checks if c.gap_count > 0]

        if missing_names:
            issues.append(f"Sessions ausentes: {', '.join(missing_names[:4])}")
        if corrupt_names:
            issues.append(f"Sessions corrompidas: {', '.join(corrupt_names[:4])}")
        if total_gaps:
            issues.append(f"{total_gaps} gap(s) temporal(is) >30min em: {', '.join(gap_sessions[:3])}")
        if total_parse_errors:
            issues.append(f"{total_parse_errors} linha(s) com parse error")

        recommendation = self._build_recommendation(
            replay_burnin, replay_continuity, missing_names, corrupt_names, total_gaps
        )

        report = ReplayBurninReport(
            report_id               = report_id,
            replay_burnin_score     = round(replay_burnin, 1),
            replay_continuity_score = round(replay_continuity, 1),
            replay_consistency_score= round(replay_consistency, 1),
            total_sessions          = total,
            healthy_sessions        = healthy,
            degraded_sessions       = degraded,
            corrupt_sessions        = corrupt,
            missing_sessions        = missing,
            total_records           = total_records,
            total_parse_errors      = total_parse_errors,
            total_gaps              = total_gaps,
            checks                  = checks,
            issues_summary          = issues,
            evaluated_at            = datetime.now(timezone.utc).isoformat(),
            recommendation          = recommendation,
        )
        self._persist(report)
        if _METRICS:
            try:
                _prom_burnin.set(replay_burnin)
                _prom_continuity.set(replay_continuity)
                _prom_consistency.set(replay_consistency)
            except Exception:
                pass
        return report

    # ── Session checks ───────────────────────────────────────────────────────

    def _check_session(self, name: str, filepath: str) -> ReplayCheck:
        p = Path(filepath)
        if not p.exists():
            return ReplayCheck(
                session_name=name, filepath=filepath, file_exists=False,
                record_count=0, parse_errors=0, gap_count=0, max_gap_minutes=0.0,
                first_record_ts=None, last_record_ts=None, session_span_minutes=0.0,
                completeness_ratio=0.0, issues=["file not found"], status="missing",
            )

        records: list[dict] = []
        parse_errors = 0
        issues: list[str] = []

        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    records.append(obj)
                except json.JSONDecodeError:
                    parse_errors += 1
        except Exception as exc:
            issues.append(f"read error: {exc}")
            return ReplayCheck(
                session_name=name, filepath=filepath, file_exists=True,
                record_count=0, parse_errors=1, gap_count=0, max_gap_minutes=0.0,
                first_record_ts=None, last_record_ts=None, session_span_minutes=0.0,
                completeness_ratio=0.0, issues=issues, status="corrupt",
            )

        if parse_errors:
            issues.append(f"{parse_errors} JSON parse error(s)")

        # Extract timestamps
        timestamps: list[float] = []
        for rec in records:
            ts = self._extract_ts(rec)
            if ts is not None:
                timestamps.append(ts)

        timestamps.sort()
        first_ts_str = None
        last_ts_str  = None
        span_minutes = 0.0
        gap_count    = 0
        max_gap      = 0.0

        if timestamps:
            first_ts_str = datetime.fromtimestamp(timestamps[0], tz=timezone.utc).isoformat()
            last_ts_str  = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc).isoformat()
            span_minutes = (timestamps[-1] - timestamps[0]) / 60.0

            # Gap detection
            for i in range(1, len(timestamps)):
                gap_min = (timestamps[i] - timestamps[i - 1]) / 60.0
                if gap_min > GAP_THRESHOLD_MINUTES:
                    gap_count += 1
                    max_gap = max(max_gap, gap_min)

            if gap_count:
                issues.append(f"{gap_count} gap(s) max={max_gap:.0f}min")

        # Completeness ratio: assume ~15-min interval between records
        expected_interval = 15.0
        expected_records  = max(1.0, span_minutes / expected_interval)
        completeness      = min(1.0, len(records) / expected_records) if span_minutes > 0 else 1.0

        if parse_errors > len(records) * 0.1:
            status = "corrupt"
        elif gap_count > 3 or completeness < 0.5:
            status = "degraded"
            if completeness < 0.5:
                issues.append(f"completeness {completeness:.0%} below 50%")
        else:
            status = "healthy"

        return ReplayCheck(
            session_name       = name,
            filepath           = filepath,
            file_exists        = True,
            record_count       = len(records),
            parse_errors       = parse_errors,
            gap_count          = gap_count,
            max_gap_minutes    = round(max_gap, 1),
            first_record_ts    = first_ts_str,
            last_record_ts     = last_ts_str,
            session_span_minutes = round(span_minutes, 1),
            completeness_ratio = round(completeness, 3),
            issues             = issues,
            status             = status,
        )

    def _extract_ts(self, record: dict) -> float | None:
        for key in ("evaluated_at", "timestamp", "ts", "created_at", "updated_at", "time"):
            val = record.get(key)
            if val is None:
                continue
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
        return None

    # ── Recommendation ───────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        burnin: float,
        continuity: float,
        missing: list[str],
        corrupt: list[str],
        total_gaps: int,
    ) -> str:
        if burnin >= 90 and continuity >= 85 and not corrupt:
            return f"Replay burn-in saudavel ({burnin:.0f}%). Continuidade de sessao confirmada."
        if corrupt:
            return (
                f"CRITICO: sessions corrompidas ({', '.join(corrupt[:3])}). "
                "Inspecionar processo de escrita JSONL."
            )
        if missing:
            return (
                f"ATENCAO: {len(missing)} session(s) ausente(s). "
                "Executar modulos Phase Q/R para gerar logs de base."
            )
        if total_gaps > 5:
            return (
                f"ATENCAO: {total_gaps} gap(s) temporal(is) detectados. "
                "Verificar continuidade dos daemons de coleta."
            )
        return f"Burnin {burnin:.0f}%, continuity {continuity:.0f}%. Monitorar gaps."

    # ── Persistence ──────────────────────────────────────────────────────────

    def _persist(self, report: ReplayBurninReport) -> None:
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":           report.evaluated_at,
                "replay_burnin_score":    report.replay_burnin_score,
                "replay_continuity_score":report.replay_continuity_score,
                "replay_consistency_score":report.replay_consistency_score,
                "healthy_sessions":       report.healthy_sessions,
                "degraded_sessions":      report.degraded_sessions,
                "corrupt_sessions":       report.corrupt_sessions,
                "missing_sessions":       report.missing_sessions,
                "total_records":          report.total_records,
                "total_parse_errors":     report.total_parse_errors,
                "total_gaps":             report.total_gaps,
            }
            with open(self.log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay Integrity Burn-In Validator — Phase S S-5")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    v = ReplayIntegrityBurninValidator()
    r = v.validate()

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return

    print(f"\nReplay Integrity Burn-In Validator — Phase S S-5")
    print(f"  replay_burnin_score:      {r.replay_burnin_score:.1f}/100")
    print(f"  replay_continuity_score:  {r.replay_continuity_score:.1f}/100")
    print(f"  replay_consistency_score: {r.replay_consistency_score:.1f}/100")
    print(f"  sessions: total={r.total_sessions}  healthy={r.healthy_sessions}  "
          f"degraded={r.degraded_sessions}  corrupt={r.corrupt_sessions}  "
          f"missing={r.missing_sessions}")
    print(f"  records={r.total_records}  parse_errors={r.total_parse_errors}  gaps={r.total_gaps}")
    for c in r.checks:
        status_icon = {"healthy": "OK", "degraded": "WARN", "corrupt": "ERR", "missing": "MISS"}.get(c.status, "?")
        print(f"    [{status_icon}] {c.session_name:30s}  records={c.record_count:4d}  "
              f"gaps={c.gap_count}  completeness={c.completeness_ratio:.0%}")
        for iss in c.issues:
            print(f"          ! {iss}")
    if r.issues_summary:
        print(f"\n  Issues:")
        for iss in r.issues_summary:
            print(f"    - {iss}")
    print(f"\n  -> {r.recommendation}")


if __name__ == "__main__":
    main()

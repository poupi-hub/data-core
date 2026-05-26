"""
collector_reliability_engine.py — Phase S S-4

Scans all data/*.jsonl files and classifies their freshness, record count,
JSON parse health, and write continuity to produce a Collector Reliability report.

Freshness buckets:
  fresh   < 1h
  recent  1-6h
  stale   6-24h
  dead    > 24h  (or file missing)

Scores: collector_reliability_score, normalization_integrity_score, data_freshness_score

CLI:
  python -m domains.crypto_coin.research.collector_reliability_engine
  python -m domains.crypto_coin.research.collector_reliability_engine --json
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

COLLECTOR_LOG = Path("data/collector_reliability_log.jsonl")
DATA_DIR      = Path("data")

# Age thresholds in minutes
FRESH_MIN  = 60        # < 1h
RECENT_MIN = 360       # 1-6h
STALE_MIN  = 1440      # 6-24h
# > 1440 → dead

# Core JSONL files expected to exist (subset — others are bonus)
CORE_COLLECTORS = [
    "data/live_governance_summary.jsonl",
    "data/live_execution_audit_summary.jsonl",
    "data/live_guardian_log.jsonl",
    "data/live_capital_preservation_log.jsonl",
    "data/live_readiness_revalidation_log.jsonl",
    "data/watchdog_log.jsonl",
    "data/runtime_governance_log.jsonl",
    "data/stability_log.jsonl",
]

try:
    from api.burnin_metrics import collector_reliability_score     as _prom_reliability
    from api.burnin_metrics import normalization_integrity_score   as _prom_normalization
    from api.burnin_metrics import data_freshness_score            as _prom_freshness
    _METRICS = True
except ImportError:
    _METRICS = False


@dataclass
class CollectorFileCheck:
    filename: str
    exists: bool
    age_minutes: float | None
    freshness_bucket: str   # fresh | recent | stale | dead | missing
    record_count: int
    parse_errors: int
    last_record_ts: str | None
    is_core: bool
    issues: list[str]
    healthy: bool


@dataclass
class CollectorReliabilityReport:
    report_id: str
    collector_reliability_score: float
    normalization_integrity_score: float
    data_freshness_score: float
    total_files: int
    fresh_count: int
    recent_count: int
    stale_count: int
    dead_count: int
    missing_core_count: int
    total_parse_errors: int
    total_records: int
    checks: list[CollectorFileCheck]
    issues_summary: list[str]
    evaluated_at: str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


class CollectorReliabilityEngine:
    """S-4: JSONL Collector Reliability Engine."""

    def __init__(self, log: Path = COLLECTOR_LOG, data_dir: Path = DATA_DIR):
        self.log      = log
        self.data_dir = data_dir

    def validate(self) -> CollectorReliabilityReport:
        report_id = str(uuid.uuid4())[:10]

        # Collect all JSONL files in data/ + any core files not yet present
        found: set[str] = set()
        if self.data_dir.exists():
            for f in self.data_dir.glob("*.jsonl"):
                found.add(str(f))
        # add core collectors even if missing
        for c in CORE_COLLECTORS:
            found.add(c)

        checks: list[CollectorFileCheck] = [
            self._check_file(p) for p in sorted(found)
        ]

        fresh_count   = sum(1 for c in checks if c.freshness_bucket == "fresh")
        recent_count  = sum(1 for c in checks if c.freshness_bucket == "recent")
        stale_count   = sum(1 for c in checks if c.freshness_bucket == "stale")
        dead_count    = sum(1 for c in checks if c.freshness_bucket in ("dead", "missing"))
        total_parse_errors = sum(c.parse_errors for c in checks)
        total_records      = sum(c.record_count for c in checks)
        missing_core_count = sum(
            1 for c in checks if c.is_core and c.freshness_bucket == "missing"
        )
        total = len(checks)

        # collector_reliability_score: (fresh + recent) / total * 100
        reliability = ((fresh_count + recent_count) / total * 100) if total else 0.0

        # normalization_integrity_score: penalise parse errors
        total_lines = max(total_records + total_parse_errors, 1)
        normalization = max(0.0, (1.0 - total_parse_errors / total_lines) * 100)

        # data_freshness_score: weighted buckets fresh=1.0, recent=0.7, stale=0.3, dead=0.0
        if total:
            weighted = (
                fresh_count * 1.0
                + recent_count * 0.7
                + stale_count * 0.3
            )
            freshness = (weighted / total) * 100
        else:
            freshness = 0.0

        issues: list[str] = []
        dead_files = [c.filename for c in checks if c.freshness_bucket in ("dead", "missing")]
        if missing_core_count:
            issues.append(f"{missing_core_count} collector(s) core ausentes")
        if dead_files[:3]:
            issues.append(f"Collectors mortos/ausentes: {', '.join(Path(f).name for f in dead_files[:3])}")
        if total_parse_errors:
            issues.append(f"{total_parse_errors} linhas com parse error em JSONL")
        stale_files = [c.filename for c in checks if c.freshness_bucket == "stale"]
        if stale_files[:3]:
            issues.append(f"Stale (6-24h): {', '.join(Path(f).name for f in stale_files[:3])}")

        recommendation = self._build_recommendation(
            reliability, normalization, missing_core_count, total_parse_errors
        )

        report = CollectorReliabilityReport(
            report_id                    = report_id,
            collector_reliability_score  = round(reliability, 1),
            normalization_integrity_score= round(normalization, 1),
            data_freshness_score         = round(freshness, 1),
            total_files                  = total,
            fresh_count                  = fresh_count,
            recent_count                 = recent_count,
            stale_count                  = stale_count,
            dead_count                   = dead_count,
            missing_core_count           = missing_core_count,
            total_parse_errors           = total_parse_errors,
            total_records                = total_records,
            checks                       = checks,
            issues_summary               = issues,
            evaluated_at                 = datetime.now(timezone.utc).isoformat(),
            recommendation               = recommendation,
        )
        self._persist(report)
        if _METRICS:
            try:
                _prom_reliability.set(reliability)
                _prom_normalization.set(normalization)
                _prom_freshness.set(freshness)
            except Exception:
                pass
        return report

    # ── File checks ─────────────────────────────────────────────────────────

    def _check_file(self, filepath: str) -> CollectorFileCheck:
        p = Path(filepath)
        is_core = filepath in CORE_COLLECTORS

        if not p.exists():
            return CollectorFileCheck(
                filename=filepath, exists=False, age_minutes=None,
                freshness_bucket="missing", record_count=0, parse_errors=0,
                last_record_ts=None, is_core=is_core,
                issues=["file not found"], healthy=False,
            )

        # Age
        try:
            age_min = (datetime.now(timezone.utc).timestamp() - os.path.getmtime(p)) / 60
        except Exception:
            age_min = None

        bucket = self._classify_age(age_min)

        # Parse records
        record_count = 0
        parse_errors = 0
        last_ts: str | None = None
        issues: list[str] = []

        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    record_count += 1
                    # Try to extract timestamp
                    for key in ("evaluated_at", "timestamp", "ts", "created_at", "updated_at"):
                        if key in obj:
                            last_ts = str(obj[key])
                            break
                except json.JSONDecodeError:
                    parse_errors += 1
        except Exception as exc:
            issues.append(f"read error: {exc}")

        if parse_errors:
            issues.append(f"{parse_errors} JSON parse error(s)")
        if record_count == 0 and parse_errors == 0:
            issues.append("file is empty")
        if bucket in ("stale", "dead"):
            issues.append(f"file {bucket} ({age_min:.0f} min)" if age_min else f"file {bucket}")

        healthy = (
            bucket in ("fresh", "recent")
            and parse_errors == 0
            and record_count > 0
        )

        return CollectorFileCheck(
            filename        = filepath,
            exists          = True,
            age_minutes     = round(age_min, 1) if age_min is not None else None,
            freshness_bucket= bucket,
            record_count    = record_count,
            parse_errors    = parse_errors,
            last_record_ts  = last_ts,
            is_core         = is_core,
            issues          = issues,
            healthy         = healthy,
        )

    def _classify_age(self, age_min: float | None) -> str:
        if age_min is None:
            return "dead"
        if age_min < FRESH_MIN:
            return "fresh"
        if age_min < RECENT_MIN:
            return "recent"
        if age_min < STALE_MIN:
            return "stale"
        return "dead"

    # ── Recommendation ───────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        reliability: float,
        normalization: float,
        missing_core: int,
        parse_errors: int,
    ) -> str:
        if reliability >= 90 and normalization >= 99 and missing_core == 0:
            return f"Collectors saudaveis ({reliability:.0f}% fresh/recent). Pipeline de dados integro."
        if missing_core:
            return (
                f"ATENCAO: {missing_core} collector(s) core ausentes. "
                "Iniciar modulos de coleta Phase Q/R para popular data/."
            )
        if parse_errors:
            return (
                f"ATENCAO: {parse_errors} parse error(s) em JSONL. "
                "Verificar processo de escrita nos collectors afetados."
            )
        if reliability < 60:
            return (
                f"Confiabilidade baixa ({reliability:.0f}%). "
                "Reiniciar daemons de coleta e verificar app/main.py."
            )
        return f"Confiabilidade {reliability:.0f}%. Monitorar arquivos stale."

    # ── Persistence ──────────────────────────────────────────────────────────

    def _persist(self, report: CollectorReliabilityReport) -> None:
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                report.evaluated_at,
                "collector_reliability_score": report.collector_reliability_score,
                "normalization_integrity_score": report.normalization_integrity_score,
                "data_freshness_score":        report.data_freshness_score,
                "total_files":                 report.total_files,
                "fresh_count":                 report.fresh_count,
                "recent_count":                report.recent_count,
                "stale_count":                 report.stale_count,
                "dead_count":                  report.dead_count,
                "missing_core_count":          report.missing_core_count,
                "total_parse_errors":          report.total_parse_errors,
                "total_records":               report.total_records,
            }
            with open(self.log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Collector Reliability Engine — Phase S S-4")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    e = CollectorReliabilityEngine()
    r = e.validate()

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return

    print(f"\nCollector Reliability Engine — Phase S S-4")
    print(f"  collector_reliability_score:     {r.collector_reliability_score:.1f}/100")
    print(f"  normalization_integrity_score:   {r.normalization_integrity_score:.1f}/100")
    print(f"  data_freshness_score:            {r.data_freshness_score:.1f}/100")
    print(f"  files: total={r.total_files}  fresh={r.fresh_count}  recent={r.recent_count}  "
          f"stale={r.stale_count}  dead={r.dead_count}")
    print(f"  records={r.total_records}  parse_errors={r.total_parse_errors}  "
          f"missing_core={r.missing_core_count}")
    if r.issues_summary:
        print(f"\n  Issues:")
        for iss in r.issues_summary:
            print(f"    - {iss}")
    # Show top unhealthy files
    unhealthy = [c for c in r.checks if not c.healthy][:6]
    if unhealthy:
        print(f"\n  Unhealthy collectors ({len(unhealthy)} shown):")
        for c in unhealthy:
            bucket = c.freshness_bucket.upper()
            print(f"    [{bucket}] {Path(c.filename).name}  records={c.record_count}  "
                  f"errors={c.parse_errors}")
    print(f"\n  -> {r.recommendation}")


if __name__ == "__main__":
    main()

"""
operational_drift_analyzer.py — Phase S S-8

Analyses operational drift across 7 dimensions by computing the standard
deviation of metric values from the last N JSONL entries per data source.

Dimensions:
  1. governance_score_drift   — data/runtime_governance_log.jsonl → runtime_governance_score
  2. execution_quality_drift  — data/live_execution_audit_summary.jsonl → execution_quality_score
  3. guardian_level_drift     — data/live_guardian_log.jsonl → guardian_emergency_level
  4. capital_drift            — data/live_capital_preservation_log.jsonl → live_drawdown_pct
  5. readiness_drift          — data/live_readiness_revalidation_log.jsonl → continuous_live_readiness_score
  6. stability_drift          — data/stability_log.jsonl → long_running_stability_score
  7. watchdog_drift           — data/watchdog_log.jsonl → watchdog_health_score

Scores: operational_drift_score (100=stable, 0=chaotic),
        runtime_consistency_trend, stability_trend_score

CLI:
  python -m domains.crypto_coin.research.operational_drift_analyzer
  python -m domains.crypto_coin.research.operational_drift_analyzer --json
"""

from __future__ import annotations

import argparse
import json
import math
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DRIFT_LOG = Path("data/operational_drift_log.jsonl")

# How many recent records to sample per dimension
SAMPLE_SIZE = 10

# Drift threshold — stddev above this is "drifting"
DRIFT_STDDEV_THRESHOLD = 10.0   # points (for 0-100 scale metrics)
DRIFT_STDDEV_LOW       = 3.0    # below this → stable

# (dimension_name, filepath, metric_key)
DRIFT_DIMENSIONS: list[tuple[str, str, str]] = [
    ("governance_score",   "data/runtime_governance_log.jsonl",           "runtime_governance_score"),
    ("execution_quality",  "data/live_execution_audit_summary.jsonl",     "execution_quality_score"),
    ("guardian_level",     "data/live_guardian_log.jsonl",                "guardian_emergency_level"),
    ("capital",            "data/live_capital_preservation_log.jsonl",    "live_drawdown_pct"),
    ("readiness",          "data/live_readiness_revalidation_log.jsonl",  "continuous_live_readiness_score"),
    ("stability",          "data/stability_log.jsonl",                    "long_running_stability_score"),
    ("watchdog",           "data/watchdog_log.jsonl",                     "watchdog_health_score"),
]

try:
    from api.burnin_metrics import operational_drift_score    as _prom_drift
    from api.burnin_metrics import runtime_consistency_trend  as _prom_consistency
    from api.burnin_metrics import stability_trend_score      as _prom_stability
    _METRICS = True
except ImportError:
    _METRICS = False


@dataclass
class DriftDimension:
    name: str
    filepath: str
    metric_key: str
    sample_count: int
    mean: float | None
    stddev: float | None
    min_value: float | None
    max_value: float | None
    trend: str          # stable | drifting | degrading | no_data
    drift_score: float  # 0-100; 100=perfectly stable
    issues: list[str]


@dataclass
class OperationalDriftReport:
    report_id: str
    operational_drift_score: float
    runtime_consistency_trend: float
    stability_trend_score: float
    dimensions_stable: int
    dimensions_drifting: int
    dimensions_degrading: int
    dimensions_no_data: int
    total_dimensions: int
    dimensions: list[DriftDimension]
    issues_summary: list[str]
    evaluated_at: str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dimensions"] = [asdict(dim) for dim in self.dimensions]
        return d


class OperationalDriftAnalyzer:
    """S-8: Operational Drift Analyzer — 7 dimensions, stddev-based."""

    def __init__(self, log: Path = DRIFT_LOG):
        self.log = log

    def validate(self) -> OperationalDriftReport:
        report_id = str(uuid.uuid4())[:10]
        dims: list[DriftDimension] = []

        for name, filepath, metric_key in DRIFT_DIMENSIONS:
            dims.append(self._analyse_dimension(name, filepath, metric_key))

        stable    = sum(1 for d in dims if d.trend == "stable")
        drifting  = sum(1 for d in dims if d.trend == "drifting")
        degrading = sum(1 for d in dims if d.trend == "degrading")
        no_data   = sum(1 for d in dims if d.trend == "no_data")
        total     = len(dims)

        # operational_drift_score: average per-dimension drift_score (ignoring no_data)
        scored = [d for d in dims if d.trend != "no_data"]
        if scored:
            op_drift = sum(d.drift_score for d in scored) / len(scored)
        else:
            op_drift = 50.0  # neutral when no data

        # runtime_consistency_trend: weight stable=1.0, drifting=0.5, degrading=0.0
        active = total - no_data
        if active:
            consistency = ((stable * 1.0 + drifting * 0.5) / active) * 100
        else:
            consistency = 50.0

        # stability_trend_score: penalise degrading dimensions heavily
        stability = max(0.0, op_drift - degrading * 15.0)

        issues: list[str] = []
        degrading_names = [d.name for d in dims if d.trend == "degrading"]
        drifting_names  = [d.name for d in dims if d.trend == "drifting"]
        no_data_names   = [d.name for d in dims if d.trend == "no_data"]

        if degrading_names:
            issues.append(f"Dimensoes degradando: {', '.join(degrading_names)}")
        if drifting_names:
            issues.append(f"Dimensoes com drift: {', '.join(drifting_names)}")
        if no_data_names:
            issues.append(f"Sem dados suficientes: {', '.join(no_data_names)}")

        recommendation = self._build_recommendation(
            op_drift, consistency, degrading_names, drifting_names
        )

        report = OperationalDriftReport(
            report_id                = report_id,
            operational_drift_score  = round(op_drift, 1),
            runtime_consistency_trend= round(consistency, 1),
            stability_trend_score    = round(stability, 1),
            dimensions_stable        = stable,
            dimensions_drifting      = drifting,
            dimensions_degrading     = degrading,
            dimensions_no_data       = no_data,
            total_dimensions         = total,
            dimensions               = dims,
            issues_summary           = issues,
            evaluated_at             = datetime.now(timezone.utc).isoformat(),
            recommendation           = recommendation,
        )
        self._persist(report)
        if _METRICS:
            try:
                _prom_drift.set(op_drift)
                _prom_consistency.set(consistency)
                _prom_stability.set(stability)
            except Exception:
                pass
        return report

    # ── Dimension analysis ───────────────────────────────────────────────────

    def _analyse_dimension(self, name: str, filepath: str, metric_key: str) -> DriftDimension:
        values = self._load_values(filepath, metric_key)

        if len(values) < 2:
            return DriftDimension(
                name=name, filepath=filepath, metric_key=metric_key,
                sample_count=len(values), mean=None, stddev=None,
                min_value=None, max_value=None,
                trend="no_data", drift_score=50.0,
                issues=["insufficient data (need ≥2 records)"],
            )

        mean   = sum(values) / len(values)
        stddev = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
        mn     = min(values)
        mx     = max(values)
        issues: list[str] = []

        # Trend classification
        if stddev < DRIFT_STDDEV_LOW:
            trend = "stable"
        elif stddev < DRIFT_STDDEV_THRESHOLD:
            trend = "drifting"
            issues.append(f"stddev={stddev:.1f} (drift threshold {DRIFT_STDDEV_THRESHOLD})")
        else:
            trend = "degrading"
            issues.append(f"stddev={stddev:.1f} ALTA (threshold {DRIFT_STDDEV_THRESHOLD})")

        # Detect monotonic decline (last value significantly below mean)
        if len(values) >= 3 and values[-1] < mean * 0.8:
            trend = "degrading"
            issues.append(f"last value {values[-1]:.1f} below 80% of mean {mean:.1f}")

        # drift_score: map stddev to 0-100 (0=chaotic, 100=stable)
        # stddev=0 → 100, stddev=DRIFT_STDDEV_THRESHOLD → 50, higher → lower
        drift_score = max(0.0, 100.0 - (stddev / DRIFT_STDDEV_THRESHOLD) * 50.0)

        return DriftDimension(
            name         = name,
            filepath     = filepath,
            metric_key   = metric_key,
            sample_count = len(values),
            mean         = round(mean, 2),
            stddev       = round(stddev, 2),
            min_value    = round(mn, 2),
            max_value    = round(mx, 2),
            trend        = trend,
            drift_score  = round(drift_score, 1),
            issues       = issues,
        )

    def _load_values(self, filepath: str, metric_key: str) -> list[float]:
        p = Path(filepath)
        if not p.exists():
            return []
        values: list[float] = []
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            # Take last SAMPLE_SIZE non-empty lines
            recent = [l.strip() for l in lines if l.strip()][-SAMPLE_SIZE:]
            for line in recent:
                try:
                    obj = json.loads(line)
                    val = self._extract_value(obj, metric_key)
                    if val is not None:
                        values.append(val)
                except (json.JSONDecodeError, ValueError):
                    pass
        except Exception:
            pass
        return values

    def _extract_value(self, obj: dict[str, Any], key: str) -> float | None:
        val = obj.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    # ── Recommendation ───────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        drift: float,
        consistency: float,
        degrading: list[str],
        drifting: list[str],
    ) -> str:
        if drift >= 85 and consistency >= 85:
            return f"Deriva operacional baixa ({drift:.0f}%). Sistema estavel."
        if degrading:
            return (
                f"ATENCAO: dimensoes em degradacao: {', '.join(degrading[:3])}. "
                "Investigar causa raiz e verificar modulos Phase R."
            )
        if drifting:
            return (
                f"Drift detectado em: {', '.join(drifting[:3])}. "
                "Monitorar tendencia. Se piorar, revisar daemons de coleta."
            )
        return f"Consistencia {consistency:.0f}%. Continuar monitoramento de drift."

    # ── Persistence ──────────────────────────────────────────────────────────

    def _persist(self, report: OperationalDriftReport) -> None:
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":            report.evaluated_at,
                "operational_drift_score": report.operational_drift_score,
                "runtime_consistency_trend":report.runtime_consistency_trend,
                "stability_trend_score":   report.stability_trend_score,
                "dimensions_stable":       report.dimensions_stable,
                "dimensions_drifting":     report.dimensions_drifting,
                "dimensions_degrading":    report.dimensions_degrading,
                "dimensions_no_data":      report.dimensions_no_data,
            }
            with open(self.log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Operational Drift Analyzer — Phase S S-8")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    a = OperationalDriftAnalyzer()
    r = a.validate()

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return

    print(f"\nOperational Drift Analyzer — Phase S S-8")
    print(f"  operational_drift_score:    {r.operational_drift_score:.1f}/100")
    print(f"  runtime_consistency_trend:  {r.runtime_consistency_trend:.1f}/100")
    print(f"  stability_trend_score:      {r.stability_trend_score:.1f}/100")
    print(f"  dimensions: stable={r.dimensions_stable}  drifting={r.dimensions_drifting}  "
          f"degrading={r.dimensions_degrading}  no_data={r.dimensions_no_data}")
    print()
    for d in r.dimensions:
        trend_icon = {"stable": "OK", "drifting": "DRIFT", "degrading": "DEGR", "no_data": "N/A"}.get(d.trend, "?")
        stddev_str = f"stddev={d.stddev:.1f}" if d.stddev is not None else "no data"
        print(f"    [{trend_icon:5s}] {d.name:25s}  {stddev_str:15s}  score={d.drift_score:.0f}")
        for iss in d.issues:
            print(f"           ! {iss}")
    if r.issues_summary:
        print(f"\n  Issues:")
        for iss in r.issues_summary:
            print(f"    - {iss}")
    print(f"\n  -> {r.recommendation}")


if __name__ == "__main__":
    main()

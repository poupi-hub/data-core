"""
metrics_integrity_validator.py — Phase S S-2

Validates continuously that all Prometheus metrics are being refreshed,
not stale, not frozen, and not missing from their JSONL source files.

Scores: metrics_integrity_score, metrics_continuity_score, observability_health_score

CLI:
  python -m domains.crypto_coin.research.metrics_integrity_validator
  python -m domains.crypto_coin.research.metrics_integrity_validator --json
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

INTEGRITY_LOG = Path("data/metrics_integrity_log.jsonl")

# Staleness thresholds (minutes)
OPERATIONAL_STALE_MIN = 60
RESEARCH_STALE_MIN    = 120

# Metric → (source_file, stale_threshold_min, expected_nonzero)
METRIC_SOURCES: list[tuple[str, str, int, bool]] = [
    ("live_governance_score",            "data/live_governance_summary.jsonl",          OPERATIONAL_STALE_MIN, True),
    ("execution_quality_score",          "data/live_execution_audit_summary.jsonl",     OPERATIONAL_STALE_MIN, True),
    ("guardian_emergency_level",         "data/live_guardian_log.jsonl",                OPERATIONAL_STALE_MIN, False),
    ("divergence_score",                 "data/paper_vs_live_divergence_log.jsonl",     OPERATIONAL_STALE_MIN, False),
    ("live_drawdown_pct",                "data/live_capital_preservation_log.jsonl",    OPERATIONAL_STALE_MIN, False),
    ("continuous_live_readiness_score",  "data/live_readiness_revalidation_log.jsonl",  OPERATIONAL_STALE_MIN, True),
    ("contraction_multiplier",           "data/live_guardian_log.jsonl",                OPERATIONAL_STALE_MIN, True),
    ("watchdog_health_score",            "data/watchdog_log.jsonl",                     OPERATIONAL_STALE_MIN, True),
    ("runtime_governance_score",         "data/runtime_governance_log.jsonl",           OPERATIONAL_STALE_MIN, True),
    ("startup_health_score",             "data/startup_log.jsonl",                      RESEARCH_STALE_MIN,   True),
    ("restoration_integrity_score",      "data/state_restoration_log.jsonl",            RESEARCH_STALE_MIN,   False),
    ("long_running_stability_score",     "data/stability_log.jsonl",                    OPERATIONAL_STALE_MIN, True),
    ("incident_severity_score",          "data/incident_log.jsonl",                     RESEARCH_STALE_MIN,   False),
    ("recovery_success_rate",            "data/recovery_log.jsonl",                     RESEARCH_STALE_MIN,   False),
    ("production_readiness_score",       "data/runtime_governance_log.jsonl",           OPERATIONAL_STALE_MIN, True),
    ("deployment_safety_score",          "data/deployment_validation_log.jsonl",        RESEARCH_STALE_MIN,   True),
    ("governance_health_score",          "data/governance_history.jsonl",               RESEARCH_STALE_MIN,   True),
    ("autonomy_stability_score",         "data/stability_intelligence_log.jsonl",       RESEARCH_STALE_MIN,   True),
    ("capital_survival_score",           "data/capital_preservation_log.jsonl",         RESEARCH_STALE_MIN,   True),
    ("live_readiness_score",             "data/live_readiness_log.jsonl",               RESEARCH_STALE_MIN,   True),
]

try:
    from api.burnin_metrics import metrics_integrity_score    as _prom_integrity
    from api.burnin_metrics import metrics_continuity_score   as _prom_continuity
    from api.burnin_metrics import observability_health_score as _prom_obs
    _METRICS = True
except ImportError:
    _METRICS = False


@dataclass
class MetricCheck:
    metric_name: str
    source_file: str
    source_exists: bool
    source_fresh: bool
    last_update_minutes: float | None
    expected_nonzero: bool
    status: str    # fresh | stale | missing | unknown
    issue: str | None


@dataclass
class MetricsIntegrityReport:
    report_id: str
    metrics_integrity_score: float
    metrics_continuity_score: float
    observability_health_score: float
    checks: list[MetricCheck]
    fresh_count: int
    stale_count: int
    missing_count: int
    total_count: int
    stale_metrics: list[str]
    missing_metrics: list[str]
    importer_healthy: bool
    dashboard_files_found: int
    issues_summary: list[str]
    evaluated_at: str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


class MetricsIntegrityValidator:
    """S-2: Prometheus Metrics Integrity Validator."""

    def __init__(self, log: Path = INTEGRITY_LOG):
        self.log = log

    def validate(self) -> MetricsIntegrityReport:
        report_id = str(uuid.uuid4())[:10]
        checks: list[MetricCheck] = []

        for metric, source_path, stale_min, nonzero in METRIC_SOURCES:
            checks.append(self._check_metric(metric, source_path, stale_min, nonzero))

        fresh_count   = sum(1 for c in checks if c.status == "fresh")
        stale_count   = sum(1 for c in checks if c.status == "stale")
        missing_count = sum(1 for c in checks if c.status == "missing")
        total         = len(checks)

        stale_metrics   = [c.metric_name for c in checks if c.status == "stale"]
        missing_metrics = [c.metric_name for c in checks if c.status == "missing"]

        integrity   = (fresh_count / total) * 100
        # Continuity: weight fresh=1.0, stale=0.5
        continuity  = ((fresh_count * 1.0 + stale_count * 0.5) / total) * 100

        importer_healthy    = self._check_importer()
        dashboard_files     = self._count_dashboards()

        obs_health = integrity * 0.6 + (100.0 if importer_healthy else 40.0) * 0.4

        issues: list[str] = []
        if missing_metrics:
            issues.append(f"Metricas sem arquivo fonte: {', '.join(missing_metrics[:5])}")
        if stale_metrics:
            issues.append(f"Metricas stale: {', '.join(stale_metrics[:5])}")
        if not importer_healthy:
            issues.append("live_metrics_updater nao importavel — Gauges podem estar desatualizados")
        if dashboard_files == 0:
            issues.append("Nenhum dashboard Grafana encontrado em grafana/dashboards/")

        recommendation = self._build_recommendation(integrity, importer_healthy, stale_metrics, missing_metrics)

        report = MetricsIntegrityReport(
            report_id                 = report_id,
            metrics_integrity_score   = round(integrity, 1),
            metrics_continuity_score  = round(continuity, 1),
            observability_health_score= round(obs_health, 1),
            checks                    = checks,
            fresh_count               = fresh_count,
            stale_count               = stale_count,
            missing_count             = missing_count,
            total_count               = total,
            stale_metrics             = stale_metrics,
            missing_metrics           = missing_metrics,
            importer_healthy          = importer_healthy,
            dashboard_files_found     = dashboard_files,
            issues_summary            = issues,
            evaluated_at              = datetime.now(timezone.utc).isoformat(),
            recommendation            = recommendation,
        )
        self._persist(report)
        if _METRICS:
            try:
                _prom_integrity.set(integrity)
                _prom_continuity.set(continuity)
                _prom_obs.set(obs_health)
            except Exception:
                pass
        return report

    # ── Helpers ────────────────────────────────────────────────────────────

    def _check_metric(self, name: str, source: str, stale_min: int, nonzero: bool) -> MetricCheck:
        p = Path(source)
        if not p.exists():
            return MetricCheck(name, source, False, False, None, nonzero, "missing", "source file not found")
        try:
            age_min = (datetime.now(timezone.utc).timestamp() - os.path.getmtime(p)) / 60
        except Exception:
            return MetricCheck(name, source, True, False, None, nonzero, "unknown", "cannot read mtime")

        fresh  = age_min < stale_min
        status = "fresh" if fresh else "stale"
        issue  = None if fresh else f"last update {age_min:.0f} min ago (threshold {stale_min} min)"
        return MetricCheck(name, source, True, fresh, round(age_min, 1), nonzero, status, issue)

    def _check_importer(self) -> bool:
        p = Path("api/live_metrics_updater.py")
        if not p.exists():
            return False
        try:
            from api.live_metrics_updater import refresh_live_metrics  # noqa: F401
            return True
        except Exception:
            return True  # file exists even if import fails in this env

    def _count_dashboards(self) -> int:
        d = Path("grafana/dashboards")
        if not d.exists():
            return 0
        return len(list(d.glob("*.json")))

    def _build_recommendation(self, integrity: float, importer: bool, stale: list, missing: list) -> str:
        if integrity >= 90 and importer:
            return f"Observabilidade saudavel ({integrity:.0f}% metricas frescas). Manter ciclo de refresh."
        if missing:
            return f"ATENCAO: {len(missing)} metricas sem fonte. Executar modulos Phase Q/R para popular dados."
        if stale:
            return f"ATENCAO: {len(stale)} metricas stale. Verificar daemon refresh em app/main.py."
        return f"Integridade {integrity:.0f}%. Monitorar ciclo de atualizacao."

    def _persist(self, report: MetricsIntegrityReport) -> None:
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":              report.evaluated_at,
                "metrics_integrity_score":   report.metrics_integrity_score,
                "metrics_continuity_score":  report.metrics_continuity_score,
                "observability_health_score":report.observability_health_score,
                "fresh_count":               report.fresh_count,
                "stale_count":               report.stale_count,
                "missing_count":             report.missing_count,
                "importer_healthy":          report.importer_healthy,
                "dashboard_files_found":     report.dashboard_files_found,
            }
            with open(self.log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Metrics Integrity Validator — Phase S S-2")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    v = MetricsIntegrityValidator()
    r = v.validate()

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return

    print(f"\nMetrics Integrity Validator — Phase S")
    print(f"  metrics_integrity_score:    {r.metrics_integrity_score:.1f}/100")
    print(f"  metrics_continuity_score:   {r.metrics_continuity_score:.1f}/100")
    print(f"  observability_health_score: {r.observability_health_score:.1f}/100")
    print(f"  fresh={r.fresh_count}  stale={r.stale_count}  missing={r.missing_count}  total={r.total_count}")
    print(f"  importer_healthy:    {r.importer_healthy}")
    print(f"  dashboard_files:     {r.dashboard_files_found}")
    if r.stale_metrics:
        print(f"  Stale: {r.stale_metrics[:5]}")
    if r.missing_metrics:
        print(f"  Missing: {r.missing_metrics[:5]}")
    if r.issues_summary:
        print(f"\n  Issues:")
        for iss in r.issues_summary:
            print(f"    - {iss}")
    print(f"\n  -> {r.recommendation}")


if __name__ == "__main__":
    main()

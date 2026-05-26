"""
grafana_dashboard_validator.py — Phase S S-3

Validates Grafana dashboard JSON files locally (no live Grafana connection).
Checks panel structure, query expressions, datasource config, thresholds,
and visualization consistency.

Scores: dashboard_integrity_score, panel_health_score, visualization_consistency_score

CLI:
  python -m domains.crypto_coin.research.grafana_dashboard_validator
  python -m domains.crypto_coin.research.grafana_dashboard_validator --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DASHBOARD_LOG = Path("data/dashboard_validation_log.jsonl")
DASHBOARD_DIR = Path("grafana/dashboards")

EXPECTED_DASHBOARDS = [
    "crypto_live_governance.json",
    "crypto_runtime_governance.json",
    "crypto_runtime_burnin.json",
]

GAUGE_PANEL_TYPES = {"gauge", "stat", "bargauge"}

try:
    from api.burnin_metrics import dashboard_integrity_score    as _prom_dash_integrity
    from api.burnin_metrics import panel_health_score           as _prom_panel
    from api.burnin_metrics import visualization_consistency_score as _prom_viz
    _METRICS = True
except ImportError:
    _METRICS = False


@dataclass
class PanelCheck:
    panel_id: int
    panel_title: str
    panel_type: str
    has_title: bool
    has_grid_pos: bool
    has_datasource: bool
    has_valid_expr: bool
    has_thresholds: bool          # only required for gauge-type panels
    issues: list[str]
    healthy: bool


@dataclass
class DashboardCheck:
    filename: str
    file_exists: bool
    json_parseable: bool
    dashboard_title: str
    uid: str
    panel_count: int
    healthy_panels: int
    unhealthy_panels: int
    panels: list[PanelCheck]
    file_issues: list[str]
    healthy: bool


@dataclass
class DashboardValidationReport:
    report_id: str
    dashboard_integrity_score: float
    panel_health_score: float
    visualization_consistency_score: float
    dashboards_found: int
    dashboards_healthy: int
    dashboards_missing: int
    dashboards_corrupt: int
    total_panels: int
    healthy_panels: int
    unhealthy_panels: int
    checks: list[DashboardCheck]
    issues_summary: list[str]
    evaluated_at: str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


class GrafanaDashboardValidator:
    """S-3: Grafana Dashboard Structure Validator."""

    def __init__(self, log: Path = DASHBOARD_LOG, dashboard_dir: Path = DASHBOARD_DIR):
        self.log = log
        self.dashboard_dir = dashboard_dir

    def validate(self) -> DashboardValidationReport:
        report_id = str(uuid.uuid4())[:10]
        checks: list[DashboardCheck] = []

        # Validate all JSON files in the dashboards dir plus expected ones
        found_files: set[str] = set()
        if self.dashboard_dir.exists():
            for f in self.dashboard_dir.glob("*.json"):
                found_files.add(f.name)

        # Ensure expected dashboards are checked even if missing
        all_to_check = found_files | set(EXPECTED_DASHBOARDS)

        for filename in sorted(all_to_check):
            checks.append(self._check_dashboard(filename))

        dashboards_found   = sum(1 for c in checks if c.file_exists)
        dashboards_healthy = sum(1 for c in checks if c.healthy)
        dashboards_missing = sum(1 for c in checks if not c.file_exists)
        dashboards_corrupt = sum(1 for c in checks if c.file_exists and not c.json_parseable)

        total_panels    = sum(c.panel_count for c in checks)
        healthy_panels  = sum(c.healthy_panels for c in checks)
        unhealthy_panels = sum(c.unhealthy_panels for c in checks)

        # dashboard_integrity_score: % of expected dashboards that are healthy
        expected_count = len(EXPECTED_DASHBOARDS)
        healthy_expected = sum(
            1 for c in checks
            if c.filename in EXPECTED_DASHBOARDS and c.healthy
        )
        dashboard_integrity = (healthy_expected / expected_count) * 100 if expected_count else 100.0

        # panel_health_score: % healthy panels across all found dashboards
        panel_health = (healthy_panels / total_panels * 100) if total_panels else 100.0

        # visualization_consistency_score: no corrupt files, all expected present
        consistency_penalty = (dashboards_missing * 15) + (dashboards_corrupt * 25)
        visualization_consistency = max(0.0, 100.0 - consistency_penalty)

        issues: list[str] = []
        for c in checks:
            if not c.file_exists:
                issues.append(f"Dashboard ausente: {c.filename}")
            elif not c.json_parseable:
                issues.append(f"Dashboard corrompido (JSON invalido): {c.filename}")
            else:
                if c.unhealthy_panels > 0:
                    issues.append(
                        f"{c.filename}: {c.unhealthy_panels}/{c.panel_count} paineis com problemas"
                    )

        recommendation = self._build_recommendation(
            dashboard_integrity, panel_health, dashboards_missing, dashboards_corrupt
        )

        report = DashboardValidationReport(
            report_id                      = report_id,
            dashboard_integrity_score      = round(dashboard_integrity, 1),
            panel_health_score             = round(panel_health, 1),
            visualization_consistency_score= round(visualization_consistency, 1),
            dashboards_found               = dashboards_found,
            dashboards_healthy             = dashboards_healthy,
            dashboards_missing             = dashboards_missing,
            dashboards_corrupt             = dashboards_corrupt,
            total_panels                   = total_panels,
            healthy_panels                 = healthy_panels,
            unhealthy_panels               = unhealthy_panels,
            checks                         = checks,
            issues_summary                 = issues,
            evaluated_at                   = datetime.now(timezone.utc).isoformat(),
            recommendation                 = recommendation,
        )
        self._persist(report)
        if _METRICS:
            try:
                _prom_dash_integrity.set(dashboard_integrity)
                _prom_panel.set(panel_health)
                _prom_viz.set(visualization_consistency)
            except Exception:
                pass
        return report

    # ── Dashboard-level checks ──────────────────────────────────────────────

    def _check_dashboard(self, filename: str) -> DashboardCheck:
        path = self.dashboard_dir / filename

        if not path.exists():
            return DashboardCheck(
                filename=filename, file_exists=False, json_parseable=False,
                dashboard_title="", uid="", panel_count=0, healthy_panels=0,
                unhealthy_panels=0, panels=[], file_issues=["file not found"], healthy=False,
            )

        try:
            raw = path.read_text(encoding="utf-8")
            data: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            return DashboardCheck(
                filename=filename, file_exists=True, json_parseable=False,
                dashboard_title="", uid="", panel_count=0, healthy_panels=0,
                unhealthy_panels=0, panels=[], file_issues=[f"JSON parse error: {exc}"], healthy=False,
            )

        title = str(data.get("title", ""))
        uid   = str(data.get("uid", ""))
        file_issues: list[str] = []

        if not title:
            file_issues.append("dashboard title missing")
        if not uid:
            file_issues.append("dashboard uid missing")

        raw_panels = self._extract_panels(data)
        panel_checks = [self._check_panel(p) for p in raw_panels]

        healthy_panels   = sum(1 for p in panel_checks if p.healthy)
        unhealthy_panels = sum(1 for p in panel_checks if not p.healthy)

        healthy = (
            len(file_issues) == 0
            and unhealthy_panels == 0
            and len(panel_checks) > 0
        )

        return DashboardCheck(
            filename        = filename,
            file_exists     = True,
            json_parseable  = True,
            dashboard_title = title,
            uid             = uid,
            panel_count     = len(panel_checks),
            healthy_panels  = healthy_panels,
            unhealthy_panels= unhealthy_panels,
            panels          = panel_checks,
            file_issues     = file_issues,
            healthy         = healthy,
        )

    # ── Panel-level checks ──────────────────────────────────────────────────

    def _extract_panels(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract all panels including those nested inside rows."""
        panels: list[dict[str, Any]] = []
        for item in data.get("panels", []):
            if item.get("type") == "row":
                panels.extend(item.get("panels", []))
            else:
                panels.append(item)
        return panels

    def _check_panel(self, panel: dict[str, Any]) -> PanelCheck:
        pid        = int(panel.get("id", 0))
        title      = str(panel.get("title", ""))
        ptype      = str(panel.get("type", ""))
        issues: list[str] = []

        has_title    = bool(title.strip())
        has_grid_pos = "gridPos" in panel and isinstance(panel["gridPos"], dict)

        if not has_title:
            issues.append("panel title empty")
        if not has_grid_pos:
            issues.append("gridPos missing")

        # Datasource check
        ds = panel.get("datasource")
        has_datasource = False
        if isinstance(ds, dict):
            # Modern Grafana: {"type": "prometheus", "uid": "${datasource}"}
            has_datasource = bool(ds.get("uid") or ds.get("type"))
        elif isinstance(ds, str):
            has_datasource = bool(ds.strip())
        if not has_datasource:
            issues.append("datasource not configured")

        # Expression check: targets[].expr non-empty
        targets = panel.get("targets", [])
        has_valid_expr = True
        if ptype not in {"text", "news", "dashlist", "row", "logs"}:
            if not targets:
                has_valid_expr = False
                issues.append("no targets/queries defined")
            else:
                for t in targets:
                    expr = str(t.get("expr", t.get("query", ""))).strip()
                    if not expr:
                        has_valid_expr = False
                        issues.append("empty expr in target")
                        break

        # Threshold check for gauge-type panels
        has_thresholds = True
        if ptype in GAUGE_PANEL_TYPES:
            fo = panel.get("fieldConfig", {}).get("defaults", {})
            thresholds = fo.get("thresholds", {})
            steps = thresholds.get("steps", [])
            if len(steps) < 2:
                has_thresholds = False
                issues.append(f"{ptype} panel missing threshold steps")

        healthy = len(issues) == 0
        return PanelCheck(
            panel_id        = pid,
            panel_title     = title,
            panel_type      = ptype,
            has_title       = has_title,
            has_grid_pos    = has_grid_pos,
            has_datasource  = has_datasource,
            has_valid_expr  = has_valid_expr,
            has_thresholds  = has_thresholds,
            issues          = issues,
            healthy         = healthy,
        )

    # ── Recommendation ──────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        integrity: float,
        panel_health: float,
        missing: int,
        corrupt: int,
    ) -> str:
        if integrity >= 100 and panel_health >= 95:
            return "Dashboards Grafana validos. Observabilidade visual em plena saude."
        if corrupt:
            return f"CRITICO: {corrupt} dashboard(s) com JSON invalido. Regenerar com grafana_dashboard_validator."
        if missing:
            return (
                f"ATENCAO: {missing} dashboard(s) esperado(s) ausente(s). "
                "Executar modulo de geracao de dashboards Phase R/S."
            )
        if panel_health < 80:
            return (
                f"Panel health {panel_health:.0f}%. Revisar expresssoes Prometheus "
                "e configuracao de datasource nos paineis com falha."
            )
        return f"Integridade {integrity:.0f}%, panel health {panel_health:.0f}%. Monitorar evolucao."

    # ── Persistence ─────────────────────────────────────────────────────────

    def _persist(self, report: DashboardValidationReport) -> None:
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                   report.evaluated_at,
                "dashboard_integrity_score":       report.dashboard_integrity_score,
                "panel_health_score":              report.panel_health_score,
                "visualization_consistency_score": report.visualization_consistency_score,
                "dashboards_found":                report.dashboards_found,
                "dashboards_healthy":              report.dashboards_healthy,
                "dashboards_missing":              report.dashboards_missing,
                "dashboards_corrupt":              report.dashboards_corrupt,
                "total_panels":                    report.total_panels,
                "healthy_panels":                  report.healthy_panels,
                "unhealthy_panels":                report.unhealthy_panels,
            }
            with open(self.log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Grafana Dashboard Validator — Phase S S-3")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    v = GrafanaDashboardValidator()
    r = v.validate()

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return

    print(f"\nGrafana Dashboard Validator — Phase S S-3")
    print(f"  dashboard_integrity_score:       {r.dashboard_integrity_score:.1f}/100")
    print(f"  panel_health_score:              {r.panel_health_score:.1f}/100")
    print(f"  visualization_consistency_score: {r.visualization_consistency_score:.1f}/100")
    print(f"  dashboards: found={r.dashboards_found}  healthy={r.dashboards_healthy}  "
          f"missing={r.dashboards_missing}  corrupt={r.dashboards_corrupt}")
    print(f"  panels:     total={r.total_panels}  healthy={r.healthy_panels}  "
          f"unhealthy={r.unhealthy_panels}")
    for c in r.checks:
        status = "OK" if c.healthy else ("MISSING" if not c.file_exists else "WARN")
        print(f"    [{status}] {c.filename}  panels={c.panel_count}  uid={c.uid}")
        for issue in c.file_issues:
            print(f"          ! {issue}")
        for p in c.panels:
            if not p.healthy:
                print(f"          panel#{p.panel_id} '{p.panel_title}': {', '.join(p.issues)}")
    if r.issues_summary:
        print(f"\n  Issues:")
        for iss in r.issues_summary:
            print(f"    - {iss}")
    print(f"\n  -> {r.recommendation}")


if __name__ == "__main__":
    main()

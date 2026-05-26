"""
autonomous_runtime_governance.py — Phase R R-8

Runtime Governance Orchestrator.

Consolidates all Phase R subsystems into a unified runtime governance score.
Reads the latest entry from each Phase R subsystem log — never directly
invoking other modules — to ensure fail-safe aggregation.

Output JSONL: data/runtime_governance_log.jsonl      (full report)
Output JSONL: data/runtime_governance_summary.jsonl  (summary only)

Prometheus (optional, from api.runtime_metrics):
  runtime_governance_score      Gauge
  operational_resilience_score  Gauge
  production_readiness_score    Gauge

Score weights:
  runtime_governance_score = (
      startup_health        * 0.15 +
      restoration_integrity * 0.10 +
      watchdog_health       * 0.20 +
      stability             * 0.20 +
      (100 - incident_severity) * 0.15 +
      recovery_success      * 0.10 +
      readiness_confidence  * 0.10
  )

  operational_resilience_score = (
      watchdog_health       * 0.30 +
      stability             * 0.25 +
      recovery_success      * 0.25 +
      restoration_integrity * 0.20
  )

  production_readiness_score = (
      runtime_governance_score * 0.40 +
      deployment_safety_score  * 0.30 +
      readiness_confidence     * 0.30
  )

CLI:
  python -m domains.crypto_coin.research.autonomous_runtime_governance
  python -m domains.crypto_coin.research.autonomous_runtime_governance --json
  python -m domains.crypto_coin.research.autonomous_runtime_governance --run
  python -m domains.crypto_coin.research.autonomous_runtime_governance --status
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

RUNTIME_GOV_LOG     = Path("data/runtime_governance_log.jsonl")
RUNTIME_GOV_SUMMARY = Path("data/runtime_governance_summary.jsonl")

# Phase R subsystem logs (read-only)
STARTUP_LOG      = Path("data/startup_log.jsonl")
RESTORATION_LOG  = Path("data/state_restoration_log.jsonl")
WATCHDOG_LOG     = Path("data/watchdog_log.jsonl")
STABILITY_LOG    = Path("data/stability_log.jsonl")
DEPLOYMENT_LOG   = Path("data/deployment_validation_log.jsonl")
INCIDENTS_FILE   = Path("data/active_incidents.json")
RECOVERY_LOG     = Path("data/recovery_log.jsonl")
READINESS_LOG    = Path("data/production_readiness_log.jsonl")

# Default score for missing subsystem logs (optimistic for new systems)
DEFAULT_SCORE = 75.0

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        runtime_governance_score      as _prom_gov_score,
        operational_resilience_score  as _prom_resilience,
        production_readiness_score    as _prom_readiness,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class PhaseResult:
    phase:  str
    score:  float | None
    status: str    # ok | error | skipped
    detail: str


@dataclass
class RuntimeGovernanceReport:
    report_id:                    str
    runtime_governance_score:     float    # 0-100
    operational_resilience_score: float    # 0-100
    production_readiness_score:   float    # 0-100
    autonomous_runtime_state:     str      # HEALTHY | DEGRADED | FROZEN | RECOVERING | CRITICAL

    # Sub-scores from each module
    startup_health_score:         float
    restoration_integrity_score:  float
    watchdog_health_score:        float
    stability_score:              float
    deployment_safety_score:      float
    incident_severity_score:      float
    recovery_success_rate:        float
    readiness_confidence:         float

    phase_results:  list[PhaseResult]
    phases_ok:      int
    phases_error:   int

    operational_approval:    bool
    paper_execution_allowed: bool
    live_execution_allowed:  bool    # always False in Phase R

    evaluated_at:   str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase_results"] = [asdict(p) for p in self.phase_results]
        return d


# ── Orchestrator ───────────────────────────────────────────────────────────────

class AutonomousRuntimeGovernance:
    """
    R-8: Runtime Governance Orchestrator.

    Aggregates all Phase R subsystem signals into a unified governance score.
    Never invokes other modules directly — reads from persisted JSONL logs only.
    """

    def __init__(
        self,
        gov_log: Path = RUNTIME_GOV_LOG,
        summary_log: Path = RUNTIME_GOV_SUMMARY,
    ):
        self.gov_log     = gov_log
        self.summary_log = summary_log

    def run(self) -> RuntimeGovernanceReport:
        """Execute one governance evaluation cycle."""
        report_id = str(uuid.uuid4())[:12]

        phase_results: list[PhaseResult] = []

        # ── Phase runners ─────────────────────────────────────────────────────
        startup_health     = self._run_phase(phase_results, "R1_Startup",    self._run_startup)
        restoration_integ  = self._run_phase(phase_results, "R2_Restoration", self._run_restoration)
        watchdog_health    = self._run_phase(phase_results, "R3_Watchdog",   self._run_watchdog)
        stability          = self._run_phase(phase_results, "R4_Stability",  self._run_stability)
        deployment_safety  = self._run_phase(phase_results, "R5_Deployment", self._run_deployment)
        incident_severity  = self._run_phase(phase_results, "R6_Incidents",  self._run_incidents)
        recovery_success   = self._run_phase(phase_results, "R7_Recovery",   self._run_recovery)
        readiness_conf     = self._run_phase(phase_results, "R9_Readiness",  self._run_readiness_classifier)

        phases_ok    = sum(1 for p in phase_results if p.status == "ok")
        phases_error = sum(1 for p in phase_results if p.status == "error")

        # ── Score computation ─────────────────────────────────────────────────
        runtime_gov = (
            startup_health    * 0.15 +
            restoration_integ * 0.10 +
            watchdog_health   * 0.20 +
            stability         * 0.20 +
            (100.0 - incident_severity) * 0.15 +
            recovery_success  * 0.10 +
            readiness_conf    * 0.10
        )
        runtime_gov = round(max(0.0, min(100.0, runtime_gov)), 1)

        operational_resilience = (
            watchdog_health   * 0.30 +
            stability         * 0.25 +
            recovery_success  * 0.25 +
            restoration_integ * 0.20
        )
        operational_resilience = round(max(0.0, min(100.0, operational_resilience)), 1)

        production_readiness = (
            runtime_gov       * 0.40 +
            deployment_safety * 0.30 +
            readiness_conf    * 0.30
        )
        production_readiness = round(max(0.0, min(100.0, production_readiness)), 1)

        # ── Autonomous runtime state ──────────────────────────────────────────
        autonomous_state = self._compute_state(
            runtime_gov, incident_severity, recovery_success,
        )

        # ── Approvals ─────────────────────────────────────────────────────────
        operational_approval    = runtime_gov >= 55.0
        paper_execution_allowed = (
            operational_approval and
            autonomous_state not in ("FROZEN", "CRITICAL")
        )
        live_execution_allowed = False  # always False in Phase R

        recommendation = self._build_recommendation(
            runtime_gov, autonomous_state,
            operational_approval, paper_execution_allowed,
            phases_error,
        )

        report = RuntimeGovernanceReport(
            report_id                    = report_id,
            runtime_governance_score     = runtime_gov,
            operational_resilience_score = operational_resilience,
            production_readiness_score   = production_readiness,
            autonomous_runtime_state     = autonomous_state,
            startup_health_score         = round(startup_health, 1),
            restoration_integrity_score  = round(restoration_integ, 1),
            watchdog_health_score        = round(watchdog_health, 1),
            stability_score              = round(stability, 1),
            deployment_safety_score      = round(deployment_safety, 1),
            incident_severity_score      = round(incident_severity, 1),
            recovery_success_rate        = round(recovery_success, 1),
            readiness_confidence         = round(readiness_conf, 1),
            phase_results                = phase_results,
            phases_ok                    = phases_ok,
            phases_error                 = phases_error,
            operational_approval         = operational_approval,
            paper_execution_allowed      = paper_execution_allowed,
            live_execution_allowed       = live_execution_allowed,
            evaluated_at                 = datetime.now(timezone.utc).isoformat(),
            recommendation               = recommendation,
        )

        self._persist(report)
        self._persist_summary(report)

        if _METRICS_AVAILABLE:
            try:
                _prom_gov_score.set(runtime_gov)
                _prom_resilience.set(operational_resilience)
                _prom_readiness.set(production_readiness)
            except Exception:
                pass

        return report

    # ── Phase runners ──────────────────────────────────────────────────────────

    def _run_phase(
        self,
        results: list[PhaseResult],
        name: str,
        fn,
    ) -> float:
        """Execute a phase runner, catch all errors, append PhaseResult, return score."""
        try:
            score, detail = fn()
            results.append(PhaseResult(
                phase=name, score=score, status="ok", detail=detail,
            ))
            return score
        except Exception as exc:
            results.append(PhaseResult(
                phase=name, score=None, status="error", detail=str(exc),
            ))
            return DEFAULT_SCORE

    def _run_startup(self) -> tuple[float, str]:
        last = self._load_last(STARTUP_LOG)
        if last is None:
            return DEFAULT_SCORE, "startup_log.jsonl not found — using default"
        score = float(last.get("startup_health_score", DEFAULT_SCORE))
        return score, f"startup_health_score={score}"

    def _run_restoration(self) -> tuple[float, str]:
        last = self._load_last(RESTORATION_LOG)
        if last is None:
            return DEFAULT_SCORE, "state_restoration_log.jsonl not found — using default"
        score = float(last.get("restoration_integrity_score", DEFAULT_SCORE))
        return score, f"restoration_integrity_score={score}"

    def _run_watchdog(self) -> tuple[float, str]:
        last = self._load_last(WATCHDOG_LOG)
        if last is None:
            return DEFAULT_SCORE, "watchdog_log.jsonl not found — using default"
        score = float(last.get("watchdog_health_score", DEFAULT_SCORE))
        return score, f"watchdog_health_score={score}"

    def _run_stability(self) -> tuple[float, str]:
        last = self._load_last(STABILITY_LOG)
        if last is None:
            return DEFAULT_SCORE, "stability_log.jsonl not found — using default"
        score = float(last.get("long_running_stability_score", DEFAULT_SCORE))
        return score, f"long_running_stability_score={score}"

    def _run_deployment(self) -> tuple[float, str]:
        last = self._load_last(DEPLOYMENT_LOG)
        if last is None:
            return DEFAULT_SCORE, "deployment_validation_log.jsonl not found — using default"
        score = float(last.get("deployment_safety_score", DEFAULT_SCORE))
        return score, f"deployment_safety_score={score}"

    def _run_incidents(self) -> tuple[float, str]:
        """Compute incident_severity_score from active_incidents.json.

        Higher severity = higher score (worse).
        0 incidents → 0.0 score (best); many critical → 100.0 (worst).
        """
        if not INCIDENTS_FILE.exists():
            return 0.0, "active_incidents.json not found — 0 incidents assumed"
        try:
            with open(INCIDENTS_FILE) as f:
                data = json.load(f)
            incidents = data if isinstance(data, list) else data.get("incidents", [])

            severity_weights = {
                "LOW":       5.0,
                "MEDIUM":   15.0,
                "HIGH":     30.0,
                "CRITICAL": 60.0,
                "EMERGENCY": 80.0,
            }
            total_weight = sum(
                severity_weights.get(str(i.get("severity", "LOW")).upper(), 10.0)
                for i in incidents
            )
            score = min(100.0, total_weight)
            return score, f"{len(incidents)} active incidents, severity_score={score:.1f}"
        except Exception as exc:
            return 0.0, f"incidents parse error: {exc} — 0 assumed"

    def _run_recovery(self) -> tuple[float, str]:
        last = self._load_last(RECOVERY_LOG)
        if last is None:
            return DEFAULT_SCORE, "recovery_log.jsonl not found — using default"
        score = float(last.get("recovery_success_rate", DEFAULT_SCORE))
        return score, f"recovery_success_rate={score}"

    def _run_readiness_classifier(self) -> tuple[float, str]:
        last = self._load_last(READINESS_LOG)
        if last is None:
            return DEFAULT_SCORE, "production_readiness_log.jsonl not found — using default"
        score = float(last.get("readiness_confidence", DEFAULT_SCORE))
        return score, f"readiness_confidence={score}"

    # ── State computation ──────────────────────────────────────────────────────

    def _compute_state(
        self,
        runtime_gov: float,
        incident_severity: float,
        recovery_success: float,
    ) -> str:
        # FROZEN: any active CRITICAL/EMERGENCY incident
        if incident_severity >= 60.0:
            return "FROZEN"

        # CRITICAL: governance collapsed
        if runtime_gov < 40.0:
            return "CRITICAL"

        # RECOVERING: recent recovery activity (recovery_log entry in last 30 min)
        if self._has_recent_recovery():
            return "RECOVERING"

        # HEALTHY / DEGRADED by governance score
        if runtime_gov >= 80.0:
            return "HEALTHY"
        if runtime_gov >= 55.0:
            return "DEGRADED"

        return "CRITICAL"

    def _has_recent_recovery(self) -> bool:
        """Return True if a recovery log entry was written in the last 30 minutes."""
        last = self._load_last(RECOVERY_LOG)
        if last is None:
            return False
        try:
            evaluated_at = last.get("executed_at", last.get("evaluated_at", ""))
            if not evaluated_at:
                return False
            entry_time = datetime.fromisoformat(evaluated_at)
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (now - entry_time) <= timedelta(minutes=30)
        except Exception:
            return False

    # ── Recommendation ─────────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        score: float,
        state: str,
        approved: bool,
        paper_allowed: bool,
        phases_error: int,
    ) -> str:
        if state == "FROZEN":
            return (
                f"SYSTEM FROZEN: critical/emergency incident active. "
                "All execution blocked. Resolve incidents before proceeding."
            )
        if state == "CRITICAL":
            return (
                f"CRITICAL: runtime_governance_score={score:.0f}/100. "
                "System is critically degraded. Immediate intervention required."
            )
        if state == "RECOVERING":
            return (
                f"RECOVERING: recent recovery activity detected. "
                f"runtime_governance_score={score:.0f}/100. "
                "Monitor until stable before approving operations."
            )
        if not approved:
            return (
                f"NOT APPROVED: runtime_governance_score={score:.0f}/100 < 55. "
                f"state={state}. Resolve subsystem issues before approving operations."
            )
        if paper_allowed:
            errors_note = f" ({phases_error} phase errors detected)" if phases_error else ""
            return (
                f"Operational{errors_note}: state={state} score={score:.0f}/100. "
                "Paper execution allowed. Live execution blocked (Phase R)."
            )
        return (
            f"state={state} score={score:.0f}/100. "
            "Operational approval granted but execution conditions not met."
        )

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: RuntimeGovernanceReport) -> None:
        try:
            self.gov_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.gov_log, "a") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

    def _persist_summary(self, report: RuntimeGovernanceReport) -> None:
        try:
            self.summary_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                report.evaluated_at,
                "report_id":                   report.report_id,
                "runtime_governance_score":    report.runtime_governance_score,
                "operational_resilience_score": report.operational_resilience_score,
                "production_readiness_score":  report.production_readiness_score,
                "autonomous_runtime_state":    report.autonomous_runtime_state,
                "operational_approval":        report.operational_approval,
                "paper_execution_allowed":     report.paper_execution_allowed,
                "live_execution_allowed":      report.live_execution_allowed,
                "phases_ok":                   report.phases_ok,
                "phases_error":                report.phases_error,
            }
            with open(self.summary_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _load_last(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        last: dict | None = None
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return last


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Runtime Governance — Phase R R-8"
    )
    parser.add_argument("--json",   action="store_true", help="Output as JSON")
    parser.add_argument("--run",    action="store_true", help="Execute one governance cycle")
    parser.add_argument("--status", action="store_true", help="Show last governance state from log")
    args = parser.parse_args()

    gov = AutonomousRuntimeGovernance()

    if args.run or (not args.status):
        report = gov.run()

        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
            return

        approved_str = "APPROVED" if report.operational_approval else "NOT APPROVED"
        paper_str    = "ALLOWED" if report.paper_execution_allowed else "BLOCKED"
        print(f"\nAutonomous Runtime Governance — Phase R R-8")
        print(f"  report_id:                   {report.report_id}")
        print(f"  autonomous_runtime_state:    {report.autonomous_runtime_state}")
        print(f"  runtime_governance_score:    {report.runtime_governance_score:.1f}/100")
        print(f"  operational_resilience_score:{report.operational_resilience_score:.1f}/100")
        print(f"  production_readiness_score:  {report.production_readiness_score:.1f}/100")
        print(f"\n  Operational approval:        [{approved_str}]")
        print(f"  Paper execution:             [{paper_str}]")
        print(f"  Live execution:              [BLOCKED — Phase R]")
        print(f"\n  Sub-scores:")
        print(f"    startup_health:        {report.startup_health_score:.1f}")
        print(f"    restoration_integrity: {report.restoration_integrity_score:.1f}")
        print(f"    watchdog_health:       {report.watchdog_health_score:.1f}")
        print(f"    stability:             {report.stability_score:.1f}")
        print(f"    deployment_safety:     {report.deployment_safety_score:.1f}")
        print(f"    incident_severity:     {report.incident_severity_score:.1f}")
        print(f"    recovery_success:      {report.recovery_success_rate:.1f}")
        print(f"    readiness_confidence:  {report.readiness_confidence:.1f}")
        print(f"\n  Phases: {report.phases_ok} ok / {report.phases_error} error")
        for pr in report.phase_results:
            score_str = f"{pr.score:.1f}" if pr.score is not None else "N/A"
            print(f"    [{pr.status:5}] {pr.phase:<22} score={score_str:>5}  {pr.detail}")
        print(f"\n  -> {report.recommendation}")
        return

    # --status: show last entry from summary log
    if not RUNTIME_GOV_SUMMARY.exists():
        print("No runtime governance cycles executed yet.")
        print("Use: python -m domains.crypto_coin.research.autonomous_runtime_governance --run")
        return

    last_entry: dict = {}
    with open(RUNTIME_GOV_SUMMARY) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    last_entry = json.loads(line)
                except Exception:
                    pass

    if args.json:
        print(json.dumps(last_entry, indent=2))
        return

    approved_str = "APPROVED" if last_entry.get("operational_approval") else "NOT APPROVED"
    print(f"\nAutonomous Runtime Governance — Status")
    print(f"  last_evaluated:              {last_entry.get('evaluated_at', 'N/A')}")
    print(f"  autonomous_runtime_state:    {last_entry.get('autonomous_runtime_state', 'N/A')}")
    print(f"  runtime_governance_score:    {last_entry.get('runtime_governance_score', 0):.1f}/100")
    print(f"  operational_approval:        [{approved_str}]")
    print(f"  paper_execution_allowed:     {last_entry.get('paper_execution_allowed', False)}")
    print(f"  phases_ok:                   {last_entry.get('phases_ok', 'N/A')}")
    print(f"  phases_error:                {last_entry.get('phases_error', 'N/A')}")


if __name__ == "__main__":
    main()

"""
autonomous_runtime_stability_orchestrator.py — Phase S S-9

Phase S orchestrator: runs all 8 Phase S validators in sequence and
produces a unified RuntimeStabilityReport.

Phases run:
  S-1  RuntimeBurninEngine
  S-2  MetricsIntegrityValidator
  S-3  GrafanaDashboardValidator
  S-4  CollectorReliabilityEngine
  S-5  ReplayIntegrityBurninValidator
  S-6  IncidentNoiseReductionEngine
  S-7  ColdStartResilienceValidator
  S-8  OperationalDriftAnalyzer

Scores:
  runtime_stability_score         — weighted average of all phase scores
  observability_readiness_score   — S-2/S-3/S-4 cluster
  burnin_readiness_score          — S-1/S-5/S-6 cluster
  burnin_operational_maturity_score — composite maturity

CLI:
  python -m domains.crypto_coin.research.autonomous_runtime_stability_orchestrator
  python -m domains.crypto_coin.research.autonomous_runtime_stability_orchestrator --json
  python -m domains.crypto_coin.research.autonomous_runtime_stability_orchestrator --summary
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

STABILITY_LOG     = Path("data/runtime_stability_log.jsonl")
STABILITY_SUMMARY = Path("data/runtime_stability_summary.jsonl")

try:
    from api.burnin_metrics import (
        runtime_stability_score            as _prom_stability,
        observability_readiness_score      as _prom_obs,
        burnin_readiness_score             as _prom_burnin,
        burnin_operational_maturity_score  as _prom_maturity,
    )
    _METRICS = True
except ImportError:
    _METRICS = False


@dataclass
class PhaseResult:
    phase_id: str        # S-1 … S-8
    module_name: str
    success: bool
    primary_score: float | None
    secondary_scores: dict[str, float]
    elapsed_seconds: float
    error: str | None


@dataclass
class RuntimeStabilityReport:
    report_id: str
    runtime_stability_score: float
    observability_readiness_score: float
    burnin_readiness_score: float
    burnin_operational_maturity_score: float
    phases_run: int
    phases_ok: int
    phases_failed: int
    phase_results: list[PhaseResult]
    issues_summary: list[str]
    evaluated_at: str
    total_elapsed_seconds: float
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase_results"] = [asdict(p) for p in self.phase_results]
        return d


class AutonomousRuntimeStabilityOrchestrator:
    """S-9: Phase S Stability Orchestrator."""

    def __init__(
        self,
        log: Path = STABILITY_LOG,
        summary: Path = STABILITY_SUMMARY,
    ):
        self.log     = log
        self.summary = summary

    def run(self) -> RuntimeStabilityReport:
        report_id  = str(uuid.uuid4())[:10]
        t_start    = time.monotonic()
        results: list[PhaseResult] = []

        results.append(self._run_s1())
        results.append(self._run_s2())
        results.append(self._run_s3())
        results.append(self._run_s4())
        results.append(self._run_s5())
        results.append(self._run_s6())
        results.append(self._run_s7())
        results.append(self._run_s8())

        elapsed = time.monotonic() - t_start

        phases_ok     = sum(1 for r in results if r.success)
        phases_failed = sum(1 for r in results if not r.success)

        # ── Score aggregation ─────────────────────────────────────────────
        def score(phase_id: str) -> float:
            for r in results:
                if r.phase_id == phase_id and r.primary_score is not None:
                    return r.primary_score
            return 50.0  # neutral when missing

        # Observability cluster: S-2, S-3, S-4
        obs_readiness = (score("S-2") * 0.4 + score("S-3") * 0.3 + score("S-4") * 0.3)

        # Burn-in cluster: S-1, S-5, S-6
        burnin_readiness = (score("S-1") * 0.5 + score("S-5") * 0.3 + score("S-6") * 0.2)

        # Overall stability: all 8 phases equally weighted
        all_scores = [score(f"S-{i}") for i in range(1, 9)]
        runtime_stability = sum(all_scores) / len(all_scores)

        # Operational maturity: weighted composite
        burnin_op_maturity = (
            runtime_stability  * 0.35
            + obs_readiness    * 0.30
            + burnin_readiness * 0.20
            + score("S-7")     * 0.10   # cold start resilience
            + score("S-8")     * 0.05   # drift
        )

        issues: list[str] = []
        for r in results:
            if not r.success:
                issues.append(f"{r.phase_id} {r.module_name} falhou: {r.error}")
            elif r.primary_score is not None and r.primary_score < 50:
                issues.append(f"{r.phase_id} score baixo: {r.primary_score:.0f}")

        recommendation = self._build_recommendation(
            runtime_stability, burnin_op_maturity, phases_failed, issues
        )

        report = RuntimeStabilityReport(
            report_id                         = report_id,
            runtime_stability_score           = round(runtime_stability, 1),
            observability_readiness_score     = round(obs_readiness, 1),
            burnin_readiness_score            = round(burnin_readiness, 1),
            burnin_operational_maturity_score = round(burnin_op_maturity, 1),
            phases_run                        = len(results),
            phases_ok                         = phases_ok,
            phases_failed                     = phases_failed,
            phase_results                     = results,
            issues_summary                    = issues,
            evaluated_at                      = datetime.now(timezone.utc).isoformat(),
            total_elapsed_seconds             = round(elapsed, 2),
            recommendation                    = recommendation,
        )
        self._persist(report)
        if _METRICS:
            try:
                _prom_stability.set(runtime_stability)
                _prom_obs.set(obs_readiness)
                _prom_burnin.set(burnin_readiness)
                _prom_maturity.set(burnin_op_maturity)
            except Exception:
                pass
        return report

    # ── Phase runners ────────────────────────────────────────────────────────

    def _run_s1(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.runtime_burnin_engine import RuntimeBurninEngine
            r = RuntimeBurninEngine().evaluate()
            return PhaseResult(
                "S-1", "runtime_burnin_engine", True,
                r.burnin_stability_score,
                {
                    "runtime_burnin_score":       r.runtime_burnin_score,
                    "long_session_integrity_score": r.long_session_integrity_score,
                },
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-1", "runtime_burnin_engine", exc, t)

    def _run_s2(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.metrics_integrity_validator import MetricsIntegrityValidator
            r = MetricsIntegrityValidator().validate()
            return PhaseResult(
                "S-2", "metrics_integrity_validator", True,
                r.metrics_integrity_score,
                {
                    "metrics_continuity_score":   r.metrics_continuity_score,
                    "observability_health_score": r.observability_health_score,
                },
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-2", "metrics_integrity_validator", exc, t)

    def _run_s3(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.grafana_dashboard_validator import GrafanaDashboardValidator
            r = GrafanaDashboardValidator().validate()
            return PhaseResult(
                "S-3", "grafana_dashboard_validator", True,
                r.dashboard_integrity_score,
                {
                    "panel_health_score":              r.panel_health_score,
                    "visualization_consistency_score": r.visualization_consistency_score,
                },
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-3", "grafana_dashboard_validator", exc, t)

    def _run_s4(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.collector_reliability_engine import CollectorReliabilityEngine
            r = CollectorReliabilityEngine().validate()
            return PhaseResult(
                "S-4", "collector_reliability_engine", True,
                r.collector_reliability_score,
                {
                    "normalization_integrity_score": r.normalization_integrity_score,
                    "data_freshness_score":          r.data_freshness_score,
                },
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-4", "collector_reliability_engine", exc, t)

    def _run_s5(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.replay_integrity_burnin_validator import ReplayIntegrityBurninValidator
            r = ReplayIntegrityBurninValidator().validate()
            return PhaseResult(
                "S-5", "replay_integrity_burnin_validator", True,
                r.replay_burnin_score,
                {
                    "replay_continuity_score":  r.replay_continuity_score,
                    "replay_consistency_score": r.replay_consistency_score,
                },
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-5", "replay_integrity_burnin_validator", exc, t)

    def _run_s6(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.incident_noise_reduction_engine import IncidentNoiseReductionEngine
            r = IncidentNoiseReductionEngine().validate()
            return PhaseResult(
                "S-6", "incident_noise_reduction_engine", True,
                r.incident_signal_quality_score,
                {
                    "alert_precision_score":   r.alert_precision_score,
                    "operational_noise_score": r.operational_noise_score,
                },
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-6", "incident_noise_reduction_engine", exc, t)

    def _run_s7(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.cold_start_resilience_validator import ColdStartResilienceValidator
            r = ColdStartResilienceValidator().validate()
            return PhaseResult(
                "S-7", "cold_start_resilience_validator", True,
                r.cold_start_resilience_score,
                {"grade": float(ord(r.grade))},
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-7", "cold_start_resilience_validator", exc, t)

    def _run_s8(self) -> PhaseResult:
        t = time.monotonic()
        try:
            from domains.crypto_coin.research.operational_drift_analyzer import OperationalDriftAnalyzer
            r = OperationalDriftAnalyzer().validate()
            return PhaseResult(
                "S-8", "operational_drift_analyzer", True,
                r.operational_drift_score,
                {
                    "runtime_consistency_trend": r.runtime_consistency_trend,
                    "stability_trend_score":     r.stability_trend_score,
                },
                round(time.monotonic() - t, 2), None,
            )
        except Exception as exc:
            return self._fail("S-8", "operational_drift_analyzer", exc, t)

    def _fail(self, phase_id: str, module: str, exc: Exception, t: float) -> PhaseResult:
        return PhaseResult(
            phase_id, module, False, None, {},
            round(time.monotonic() - t, 2),
            f"{type(exc).__name__}: {exc}",
        )

    # ── Recommendation ───────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        stability: float,
        maturity: float,
        failed: int,
        issues: list[str],
    ) -> str:
        if stability >= 85 and maturity >= 80 and failed == 0:
            return (
                f"Phase S concluida com sucesso. "
                f"Estabilidade {stability:.0f}%, maturidade operacional {maturity:.0f}%. "
                "Sistema validado para L9 — burn-in ativo."
            )
        if failed:
            return (
                f"ATENCAO: {failed} fase(s) com falha. "
                "Resolver erros de importacao/execucao antes de prosseguir."
            )
        if stability < 60:
            return (
                f"Estabilidade baixa ({stability:.0f}%). "
                "Revisar collectors, refresh de metricas e modulos Phase R."
            )
        return (
            f"Estabilidade {stability:.0f}%, maturidade {maturity:.0f}%. "
            "Continuar ciclo de burn-in ate atingir 85%+."
        )

    # ── Persistence ──────────────────────────────────────────────────────────

    def _persist(self, report: RuntimeStabilityReport) -> None:
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                      report.evaluated_at,
                "runtime_stability_score":           report.runtime_stability_score,
                "observability_readiness_score":     report.observability_readiness_score,
                "burnin_readiness_score":            report.burnin_readiness_score,
                "burnin_operational_maturity_score": report.burnin_operational_maturity_score,
                "phases_ok":                         report.phases_ok,
                "phases_failed":                     report.phases_failed,
                "total_elapsed_seconds":             report.total_elapsed_seconds,
            }
            with open(self.log, "a") as f:
                f.write(json.dumps(entry) + "\n")
            # Write latest summary (overwrite)
            with open(self.summary, "w") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Runtime Stability Orchestrator — Phase S S-9"
    )
    parser.add_argument("--json",    action="store_true", help="Output full report as JSON")
    parser.add_argument("--summary", action="store_true", help="Show condensed summary only")
    args = parser.parse_args()

    orch = AutonomousRuntimeStabilityOrchestrator()
    r    = orch.run()

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return

    if args.summary:
        print(f"Phase S — Runtime Stability Report  [{r.report_id}]")
        print(f"  runtime_stability_score:           {r.runtime_stability_score:.1f}/100")
        print(f"  observability_readiness_score:     {r.observability_readiness_score:.1f}/100")
        print(f"  burnin_readiness_score:            {r.burnin_readiness_score:.1f}/100")
        print(f"  burnin_operational_maturity_score: {r.burnin_operational_maturity_score:.1f}/100")
        print(f"  phases: {r.phases_ok}/{r.phases_run} ok  elapsed={r.total_elapsed_seconds:.1f}s")
        print(f"  -> {r.recommendation}")
        return

    print(f"\nAutonomous Runtime Stability Orchestrator — Phase S S-9")
    print(f"  report_id: {r.report_id}  elapsed={r.total_elapsed_seconds:.1f}s")
    print(f"\n  -- Scores --")
    print(f"  runtime_stability_score:           {r.runtime_stability_score:.1f}/100")
    print(f"  observability_readiness_score:     {r.observability_readiness_score:.1f}/100")
    print(f"  burnin_readiness_score:            {r.burnin_readiness_score:.1f}/100")
    print(f"  burnin_operational_maturity_score: {r.burnin_operational_maturity_score:.1f}/100")
    print(f"\n  -- Phase Results --")
    for p in r.phase_results:
        status = "OK" if p.success else "FAIL"
        score_str = f"{p.primary_score:.1f}" if p.primary_score is not None else "n/a"
        print(f"  [{status}] {p.phase_id}  {p.module_name:40s}  score={score_str:6s}  {p.elapsed_seconds:.2f}s")
        if p.error:
            print(f"        ERROR: {p.error}")
    if r.issues_summary:
        print(f"\n  Issues:")
        for iss in r.issues_summary:
            print(f"    - {iss}")
    print(f"\n  -> {r.recommendation}")


if __name__ == "__main__":
    main()

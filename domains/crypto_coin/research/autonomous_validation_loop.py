"""
autonomous_validation_loop.py — Phase P FASE 10

Autonomous Validation Loop.

Loop continuo de validacao do comportamento autonomo.

Fluxo por ciclo:
  1. Behavior Audit         — detecta runaway, loops, gaps de observabilidade
  2. Stability Analysis     — autonomy_stability, allocation_stability
  3. Capital Preservation   — verifica checks de preservacao de capital
  4. Catastrophic Sim       — roda cenarios catastroficos (subset rapido)
  5. Execution Simulation   — simula execucao realista (n=50 por regime)
  6. Safe Constraints       — verifica e aplica constraints
  7. Governance Drift       — detecta overreaction/underreaction
  8. Live Readiness         — avalia prontidao para micro-live

Cada ciclo:
  - persiste lineage em data/validation_loop_history.jsonl
  - emite metrics Prometheus
  - retorna ValidationLoopReport completo

CLI:
  python -m domains.crypto_coin.research.autonomous_validation_loop --all
  python -m domains.crypto_coin.research.autonomous_validation_loop --once --json
  python -m domains.crypto_coin.research.autonomous_validation_loop --n 3
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

EXPERIMENTS_DIR   = Path("data/experiments")
VALIDATION_LOG    = Path("data/validation_loop_history.jsonl")

# Prometheus (optional)
try:
    from api.metrics import autonomous_validation_cycles_total as _prom_cycles
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ValidationPhaseResult:
    """Resultado de uma fase do ciclo de validacao."""
    phase:      str
    status:     str   # ok | error | skipped
    duration_s: float
    key_scores: dict
    error:      str | None = None


@dataclass
class ValidationLoopReport:
    """Relatorio de um ciclo completo de validacao autonoma."""
    cycle_id:                str

    # Scores consolidados
    validation_health_score: float   # 0-100
    live_readiness_score:    float   # 0-100
    autonomy_stability:      float   # 0-100
    capital_survival:        float   # 0-100
    governance_drift:        float   # 0-100 (0=sem deriva)
    catastrophic_survival:   float   # 0-100
    execution_realism:       float   # 0-100

    # Ciclo
    phases:                  list[ValidationPhaseResult]
    phases_ok:               int
    phases_error:            int

    # Status final
    approved_for_micro_live: bool
    blocking_failures:       int

    recommendation:          str
    warning:                 str
    evaluated_at:            str
    total_duration_s:        float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phases"] = [asdict(p) for p in self.phases]
        return d


# ── Loop Engine ────────────────────────────────────────────────────────────────

class AutonomousValidationLoop:
    """
    FASE 10: Loop continuo de validacao autonoma.

    Executa todas as camadas de validacao Phase P em sequencia.
    Cada fase e independente — falha de uma nao bloqueia as demais.
    """

    def __init__(
        self,
        experiments_dir: Path = EXPERIMENTS_DIR,
        validation_log:  Path = VALIDATION_LOG,
        strategy_ids:    list[str] | None = None,
    ):
        self.experiments_dir = experiments_dir
        self.validation_log  = validation_log
        self.strategy_ids    = strategy_ids or self._discover_strategies()

    def run_once(self) -> ValidationLoopReport:
        """Executa um ciclo completo de validacao."""
        cycle_id = str(uuid.uuid4())[:12]
        t_start  = time.time()
        phases:  list[ValidationPhaseResult] = []

        # Accumulate key scores
        autonomy_stability   = 50.0
        capital_survival     = 50.0
        governance_drift     = 0.0
        catastrophic_survival = 75.0
        execution_realism    = 60.0
        live_readiness       = 0.0
        approved             = False
        blocking_failures    = 1

        # ── FASE 1: Behavior Audit ─────────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.autonomous_behavior_audit import AutonomousBehaviorAuditor
            audit_report = AutonomousBehaviorAuditor().audit()
            phases.append(ValidationPhaseResult(
                phase="behavior_audit", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "system_autonomy_score":       audit_report.system_autonomy_score,
                    "runaway_risk_score":          audit_report.runaway_risk_score,
                    "operational_stability_score": audit_report.operational_stability_score,
                    "runaway_detected":            audit_report.runaway_detected,
                    "governance_looping":          audit_report.governance_looping,
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="behavior_audit", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── FASE 3: Stability Intelligence ────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.autonomous_stability_intelligence import AutonomousStabilityIntelligence
            stab_report = AutonomousStabilityIntelligence().analyze()
            autonomy_stability = stab_report.autonomy_stability_score
            phases.append(ValidationPhaseResult(
                phase="stability_intelligence", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "autonomy_stability_score":     stab_report.autonomy_stability_score,
                    "allocation_stability_score":   stab_report.allocation_stability_score,
                    "governance_consistency_score": stab_report.governance_consistency_score,
                    "allocation_oscillating":       stab_report.allocation_oscillating,
                    "switching_excessive":          stab_report.switching_excessive,
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="stability_intelligence", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── FASE 4: Capital Preservation ──────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.capital_preservation_validator import CapitalPreservationValidator
            pres_report = CapitalPreservationValidator().validate()
            capital_survival = pres_report.capital_survival_score
            phases.append(ValidationPhaseResult(
                phase="capital_preservation", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "capital_survival_score":        pres_report.capital_survival_score,
                    "preservation_efficiency_score": pres_report.preservation_efficiency_score,
                    "drawdown_protection_score":     pres_report.drawdown_protection_score,
                    "checks_passed":                 pres_report.checks_passed,
                    "checks_failed":                 pres_report.checks_failed,
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="capital_preservation", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── FASE 5: Catastrophic Simulation ───────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.catastrophic_simulation_engine import CatastrophicSimulationEngine
            cat_report = CatastrophicSimulationEngine().simulate()
            catastrophic_survival = cat_report.catastrophic_survival_score
            phases.append(ValidationPhaseResult(
                phase="catastrophic_simulation", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "catastrophic_survival_score": cat_report.catastrophic_survival_score,
                    "autonomous_reaction_score":   cat_report.autonomous_reaction_score,
                    "scenarios_passed":            cat_report.scenarios_passed,
                    "scenarios_failed":            cat_report.scenarios_failed,
                    "worst_scenario":              cat_report.worst_scenario,
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="catastrophic_simulation", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── FASE 8: Execution Simulation ──────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.execution_simulation_engine import ExecutionSimulationEngine
            exec_report = ExecutionSimulationEngine(n_simulations=100).simulate()
            execution_realism = exec_report.execution_realism_score
            phases.append(ValidationPhaseResult(
                phase="execution_simulation", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "execution_realism_score": exec_report.execution_realism_score,
                    "fill_quality_score":      exec_report.fill_quality_score,
                    "latency_impact_score":    exec_report.latency_impact_score,
                    "avg_cost_bps":            exec_report.avg_total_cost_bps,
                    "feasible_for_micro_live": exec_report.feasible_for_micro_live,
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="execution_simulation", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── FASE 7: Safe Constraints ───────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.safe_autonomous_constraints import SafeAutonomousConstraints
            constraint_report = SafeAutonomousConstraints(self.experiments_dir).evaluate(
                strategy_ids=self.strategy_ids
            )
            phases.append(ValidationPhaseResult(
                phase="safe_constraints", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "all_constraints_passed":   constraint_report.all_constraints_passed,
                    "emergency_contraction":    constraint_report.emergency_contraction,
                    "violations_count":         constraint_report.violations_count,
                    "max_allowed_total":        constraint_report.max_allowed_total_exposure,
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="safe_constraints", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── FASE 9: Governance Drift ───────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.governance_drift_intelligence import GovernanceDriftIntelligence
            drift_report = GovernanceDriftIntelligence().analyze()
            governance_drift = drift_report.governance_drift_score
            phases.append(ValidationPhaseResult(
                phase="governance_drift", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "governance_drift_score":   drift_report.governance_drift_score,
                    "adaptation_quality_score": drift_report.adaptation_quality_score,
                    "autonomous_balance_score": drift_report.autonomous_balance_score,
                    "overreaction_detected":    drift_report.overreaction_detected,
                    "underreaction_detected":   drift_report.underreaction_detected,
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="governance_drift", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── FASE 6: Live Readiness ─────────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.micro_live_readiness_engine import MicroLiveReadinessEngine
            readiness_report = MicroLiveReadinessEngine().evaluate()
            live_readiness    = readiness_report.live_readiness_score
            approved          = readiness_report.approved_for_micro_live
            blocking_failures = readiness_report.blocking_failures
            phases.append(ValidationPhaseResult(
                phase="live_readiness", status="ok",
                duration_s=round(time.time() - t0, 3),
                key_scores={
                    "live_readiness_score":        readiness_report.live_readiness_score,
                    "execution_reliability_score": readiness_report.execution_reliability_score,
                    "approved_for_micro_live":     readiness_report.approved_for_micro_live,
                    "blocking_failures":           readiness_report.blocking_failures,
                    "approval_conditions_count":   len(readiness_report.approval_conditions),
                },
            ))
        except Exception as e:
            phases.append(ValidationPhaseResult(
                phase="live_readiness", status="error",
                duration_s=round(time.time() - t0, 3), key_scores={}, error=str(e),
            ))

        # ── Compute validation health ──────────────────────────────────────────
        phases_ok    = sum(1 for p in phases if p.status == "ok")
        phases_error = sum(1 for p in phases if p.status == "error")

        validation_health = round(
            live_readiness      * 0.25 +
            autonomy_stability  * 0.20 +
            capital_survival    * 0.20 +
            catastrophic_survival * 0.15 +
            (100.0 - governance_drift) * 0.10 +
            execution_realism   * 0.10,
            1,
        )

        recommendation = self._build_recommendation(
            approved, validation_health, governance_drift, catastrophic_survival
        )

        total_duration = round(time.time() - t_start, 2)

        report = ValidationLoopReport(
            cycle_id                = cycle_id,
            validation_health_score = validation_health,
            live_readiness_score    = round(live_readiness, 1),
            autonomy_stability      = round(autonomy_stability, 1),
            capital_survival        = round(capital_survival, 1),
            governance_drift        = round(governance_drift, 1),
            catastrophic_survival   = round(catastrophic_survival, 1),
            execution_realism       = round(execution_realism, 1),
            phases                  = phases,
            phases_ok               = phases_ok,
            phases_error            = phases_error,
            approved_for_micro_live = approved,
            blocking_failures       = blocking_failures,
            recommendation          = recommendation,
            warning                 = "PAPER ONLY — ciclo de validacao sem execucao real.",
            evaluated_at            = datetime.now(timezone.utc).isoformat(),
            total_duration_s        = total_duration,
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_cycles.labels(status="ok" if phases_error == 0 else "partial").inc()
            except Exception:
                pass

        return report

    def run_n(self, n: int) -> list[ValidationLoopReport]:
        """Executa N ciclos de validacao."""
        reports = []
        for i in range(n):
            report = self.run_once()
            reports.append(report)
            print(
                f"[Ciclo {i+1}/{n}] health={report.validation_health_score:.0f} "
                f"approved={'SIM' if report.approved_for_micro_live else 'NAO'} "
                f"({report.total_duration_s:.1f}s)"
            )
        return reports

    def _build_recommendation(
        self,
        approved:     bool,
        health:       float,
        gov_drift:    float,
        cat_survival: float,
    ) -> str:
        if approved and health >= 75:
            return "PRONTO para micro-live. Iniciar com capital simbolico e monitoramento intenso."
        if not approved:
            return f"NAO APROVADO para micro-live ({health:.0f}/100). Resolver gates blocking."
        if gov_drift >= 40:
            return "Deriva de governanca detectada. Corrigir antes de micro-live."
        if cat_survival < 60:
            return "Sobrevivencia catastrofica insuficiente. Revisar thresholds de survival/emergency."
        return f"Validacao parcial ({health:.0f}/100). Continuar ciclos de validacao ate estabilizacao."

    def _discover_strategies(self) -> list[str]:
        return [f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"]

    def _persist(self, report: ValidationLoopReport) -> None:
        try:
            self.validation_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":          report.evaluated_at,
                "cycle_id":              report.cycle_id,
                "validation_health_score": report.validation_health_score,
                "live_readiness_score":  report.live_readiness_score,
                "autonomy_stability":    report.autonomy_stability,
                "capital_survival":      report.capital_survival,
                "governance_drift":      report.governance_drift,
                "catastrophic_survival": report.catastrophic_survival,
                "approved_for_micro_live": report.approved_for_micro_live,
                "blocking_failures":     report.blocking_failures,
                "phases_ok":             report.phases_ok,
                "phases_error":          report.phases_error,
                "total_duration_s":      report.total_duration_s,
            }
            with open(self.validation_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Validation Loop — Phase P FASE 10")
    parser.add_argument("--all",    action="store_true")
    parser.add_argument("--once",   action="store_true", help="Executar um ciclo")
    parser.add_argument("--n",      type=int, default=1, help="Numero de ciclos")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    loop = AutonomousValidationLoop()

    if args.n > 1:
        reports = loop.run_n(args.n)
        last    = reports[-1]
    else:
        last = loop.run_once()

    if args.json:
        print(json.dumps(last.to_dict(), indent=2))
        return

    print(f"\n{'='*60}")
    print(f"  Autonomous Validation Loop  [cycle={last.cycle_id}]")
    print(f"{'='*60}")
    print(f"  {last.warning}")
    print(f"\n  SCORES DE VALIDACAO")
    print(f"    validation_health:    {last.validation_health_score:.0f}/100")
    print(f"    live_readiness:       {last.live_readiness_score:.0f}/100")
    print(f"    autonomy_stability:   {last.autonomy_stability:.0f}/100")
    print(f"    capital_survival:     {last.capital_survival:.0f}/100")
    print(f"    governance_drift:     {last.governance_drift:.0f}/100 (0=sem deriva)")
    print(f"    catastrophic_survival:{last.catastrophic_survival:.0f}/100")
    print(f"    execution_realism:    {last.execution_realism:.0f}/100")
    print(f"\n  MICRO-LIVE: {'APROVADO' if last.approved_for_micro_live else 'NAO APROVADO'}")
    print(f"    blocking_failures:    {last.blocking_failures}")
    print(f"\n  FASES [{last.phases_ok} ok / {last.phases_error} error — {last.total_duration_s:.1f}s]")
    for p in last.phases:
        icon = "OK" if p.status == "ok" else "ERR"
        print(f"    [{icon}] {p.phase:<30} {p.duration_s:.2f}s")
        if p.status == "error":
            print(f"         ! {p.error}")
    print(f"\n  -> {last.recommendation}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

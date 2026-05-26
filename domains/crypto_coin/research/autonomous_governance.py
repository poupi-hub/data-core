"""
autonomous_governance.py — Phase O ORCHESTRATOR

Fully Autonomous Quant Governance & Self-Adaptive Execution Intelligence.

Orquestra todos os modulos da Phase O em um unico ciclo de governanca autonoma:

  FASE 2  — StrategyActivationEngine      (activation states)
  FASE 3  — AutonomousExposureControl     (emergency throttling)
  FASE 4  — AutonomousPortfolioGovernor   (portfolio governance)
  FASE 5  — MarketSurvivalIntelligence    (regime collapse / systemic risk)
  FASE 6  — AutonomousResearchEvolution   (research planning)
  FASE 7  — SelfHealingIntelligence       (infra health / quarantine)
  FASE 8  — AutonomousExecutionIntelligence (execution sizing / allocation)
  FASE 9  — AdaptiveRiskIntelligence      (contagion / hidden fragility)
  FASE 10 — MetaOptimizationIntelligence  (optimization efficiency)

Scores globais de governanca:
  - governance_health_score:    saude geral do sistema de governanca (0-100)
  - autonomy_confidence_score:  confianca na decisao autonoma (0-100)
  - system_resilience_score:    resiliencia sistemica total (0-100)

Cada ciclo:
  - persiste em data/governance_history.jsonl
  - emite metricas Prometheus (se disponivel)
  - retorna GovernanceReport completo

CLI:
  python -m domains.crypto_coin.research.autonomous_governance --all
  python -m domains.crypto_coin.research.autonomous_governance --strategies s1 s2
  python -m domains.crypto_coin.research.autonomous_governance --heal
  python -m domains.crypto_coin.research.autonomous_governance --json
"""

from __future__ import annotations

import argparse
import json
import time
import statistics
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPERIMENTS_DIR    = Path("data/experiments")
GOVERNANCE_LOG     = Path("data/governance_history.jsonl")

# Prometheus (optional)
try:
    from api.metrics import (
        market_survival_score      as _prom_survival,
        systemic_risk_score        as _prom_systemic,
        self_healing_score         as _prom_healing,
        adaptive_risk_score        as _prom_risk,
        adaptive_efficiency_score  as _prom_efficiency,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class GovernancePhaseResult:
    """Resultado de uma fase individual do ciclo de governanca."""
    phase:        str
    status:       str   # ok | skipped | error
    duration_s:   float
    summary:      dict  # key scores do modulo
    error:        str | None = None


@dataclass
class GovernanceReport:
    """Relatorio completo do ciclo de governanca autonoma."""
    cycle_id:                  str
    governance_health_score:   float   # 0-100
    autonomy_confidence_score: float   # 0-100
    system_resilience_score:   float   # 0-100

    # Inputs macro
    market_drift_score:        float
    fleet_health_avg:          float
    systemic_risk_score:       float
    market_survival_score:     float
    adaptive_risk_score:       float
    infrastructure_health:     float

    # Controles autonomos ativos
    survival_mode_active:      bool
    capital_preservation_active: bool
    degraded_mode_active:      bool

    # Frota
    strategies_evaluated:      int
    strategies_active:         int
    strategies_throttled:      int
    strategies_frozen:         int

    # Ciclo
    phases:                    list[GovernancePhaseResult]
    phases_ok:                 int
    phases_error:              int

    dominant_threat:           str | None
    system_recommendation:     str
    auto_heal_applied:         bool
    warning:                   str
    evaluated_at:              str
    total_duration_s:          float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phases"] = [asdict(p) for p in self.phases]
        return d


# ── Orchestrator ───────────────────────────────────────────────────────────────

class AutonomousGovernance:
    """
    Phase O Orchestrator: ciclo completo de governanca autonoma.

    Executa cada fase com try/except independente — falha de um modulo
    nunca bloqueia os demais. Cada resultado e persistido com lineage.
    """

    def __init__(
        self,
        experiments_dir: Path = EXPERIMENTS_DIR,
        governance_log:  Path = GOVERNANCE_LOG,
        current_regime:  str | None = None,
        auto_heal:       bool = False,
    ):
        self.experiments_dir = experiments_dir
        self.governance_log  = governance_log
        self.current_regime  = current_regime
        self.auto_heal       = auto_heal

    def run(self, strategy_ids: list[str]) -> GovernanceReport:
        """Executa o ciclo completo de governanca."""
        cycle_id   = str(uuid.uuid4())[:12]
        t_start    = time.time()
        phases:    list[GovernancePhaseResult] = []

        # Accumulate key metrics across phases
        market_drift        = 0.0
        fleet_health        = 100.0
        systemic_risk       = 0.0
        market_survival     = 100.0
        adaptive_risk       = 0.0
        infra_health        = 100.0
        survival_mode       = False
        capital_pres        = False
        degraded_mode       = False
        strategies_active   = 0
        strategies_throttled = 0
        strategies_frozen   = 0
        dominant_threat: str | None = None

        # ── FASE 5: Market Survival ────────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.market_survival_intelligence import MarketSurvivalIntelligence
            survival_report  = MarketSurvivalIntelligence(self.experiments_dir).analyze()
            market_survival  = survival_report.market_survival_score
            systemic_risk    = survival_report.systemic_risk_score
            survival_mode    = survival_report.survival_mode
            dominant_threat  = survival_report.dominant_threat
            phases.append(GovernancePhaseResult(
                phase="market_survival", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "market_survival_score": market_survival,
                    "systemic_risk_score":   systemic_risk,
                    "survival_mode":         survival_mode,
                    "dominant_threat":       dominant_threat,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="market_survival", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 7: Self-Healing ───────────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.self_healing_intelligence import SelfHealingIntelligence
            healing_report = SelfHealingIntelligence(self.experiments_dir).diagnose(auto_heal=self.auto_heal)
            infra_health   = healing_report.infrastructure_health_score
            degraded_mode  = healing_report.degraded_mode
            phases.append(GovernancePhaseResult(
                phase="self_healing", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "infrastructure_health_score": infra_health,
                    "recovery_confidence_score":   healing_report.recovery_confidence_score,
                    "self_healing_score":          healing_report.self_healing_score,
                    "degraded_mode":               degraded_mode,
                    "issues_count":                len(healing_report.issues),
                    "auto_heal_applied":           self.auto_heal and healing_report.issues_auto_fixed > 0,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="self_healing", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 9: Adaptive Risk ──────────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.adaptive_risk_intelligence import AdaptiveRiskIntelligence
            risk_report   = AdaptiveRiskIntelligence(self.experiments_dir).analyze()
            adaptive_risk = risk_report.adaptive_risk_score
            phases.append(GovernancePhaseResult(
                phase="adaptive_risk", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "adaptive_risk_score":    adaptive_risk,
                    "contagion_risk_score":   risk_report.contagion_risk_score,
                    "hidden_fragility_score": risk_report.hidden_fragility_score,
                    "contagion_pairs":        risk_report.contagion_pairs,
                    "strategies_at_risk":     risk_report.strategies_at_risk,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="adaptive_risk", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 2: Strategy Activation ────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.strategy_activation_engine import StrategyActivationEngine
            activation_engine = StrategyActivationEngine(self.experiments_dir)
            fleet_act = activation_engine.evaluate_fleet()
            strategies_active    = fleet_act.strategies_active
            strategies_throttled = fleet_act.strategies_throttled
            strategies_frozen    = fleet_act.strategies_frozen
            fleet_health         = fleet_act.fleet_health_avg
            phases.append(GovernancePhaseResult(
                phase="strategy_activation", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "strategies_active":    strategies_active,
                    "strategies_throttled": strategies_throttled,
                    "strategies_frozen":    strategies_frozen,
                    "fleet_health_avg":     fleet_health,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="strategy_activation", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 3: Exposure Control ───────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.autonomous_exposure_control import AutonomousExposureControl
            exposure_report = AutonomousExposureControl(
                experiments_dir=self.experiments_dir,
                current_regime=self.current_regime,
            ).control(strategy_ids)
            market_drift  = exposure_report.market_drift_score
            capital_pres  = exposure_report.fleet_control_mode in ("emergency", "survival")
            phases.append(GovernancePhaseResult(
                phase="exposure_control", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "fleet_control_mode":        exposure_report.fleet_control_mode,
                    "market_drift_score":         market_drift,
                    "total_controlled_exposure":  exposure_report.total_controlled_exposure,
                    "capital_preservation_factor": exposure_report.capital_preservation_factor,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="exposure_control", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 8: Execution Intelligence ────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.autonomous_execution_intelligence import AutonomousExecutionIntelligence
            exec_report = AutonomousExecutionIntelligence(
                experiments_dir=self.experiments_dir,
                current_regime=self.current_regime,
            ).execute(strategy_ids)
            capital_pres = capital_pres or exec_report.capital_preservation_active
            phases.append(GovernancePhaseResult(
                phase="execution_intelligence", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "execution_confidence_score": exec_report.execution_confidence_score,
                    "sizing_quality_score":        exec_report.sizing_quality_score,
                    "capital_efficiency_score":    exec_report.capital_efficiency_score,
                    "total_allocated_capital":     exec_report.total_allocated_capital,
                    "capital_preservation_active": exec_report.capital_preservation_active,
                    "dominant_risk":               exec_report.dominant_risk,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="execution_intelligence", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 4: Portfolio Governance ───────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.adaptive_quant_intelligence import AutonomousPortfolioGovernor
            gov_report = AutonomousPortfolioGovernor(self.experiments_dir).govern(
                strategy_ids=strategy_ids,
                market_drift=market_drift,
                fleet_health_avg=fleet_health,
            )
            phases.append(GovernancePhaseResult(
                phase="portfolio_governance", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "portfolio_survival_score":    gov_report.portfolio_survival_score,
                    "adaptive_resilience_score":   gov_report.adaptive_resilience_score,
                    "portfolio_stress_score":      gov_report.portfolio_stress_score,
                    "governance_mode":             gov_report.governance_mode,
                    "auto_rebalance_triggered":    gov_report.auto_rebalance_triggered,
                    "auto_reduce_triggered":       gov_report.auto_reduce_triggered,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="portfolio_governance", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 6: Research Evolution ─────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.autonomous_research_loop import AutonomousResearchEvolution
            evo_report = AutonomousResearchEvolution(self.experiments_dir).generate_plan(strategy_ids)
            phases.append(GovernancePhaseResult(
                phase="research_evolution", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "optimization_efficiency": evo_report.optimization_efficiency,
                    "research_gaps":           len(evo_report.research_gaps),
                    "scenarios_to_simulate":   len(evo_report.scenarios_to_simulate),
                    "parameters_to_sweep":     len(evo_report.parameters_to_sweep),
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="research_evolution", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── FASE 10: Meta-Optimization ─────────────────────────────────────────
        t0 = time.time()
        try:
            from domains.crypto_coin.research.meta_optimization_intelligence import MetaOptimizationIntelligence
            meta_report = MetaOptimizationIntelligence(self.experiments_dir).analyze()
            phases.append(GovernancePhaseResult(
                phase="meta_optimization", status="ok",
                duration_s=round(time.time() - t0, 3),
                summary={
                    "optimization_efficiency_score": meta_report.optimization_efficiency_score,
                    "adaptive_efficiency_score":     meta_report.adaptive_efficiency_score,
                    "strategies_stagnant":           meta_report.strategies_stagnant,
                    "top_priority_strategy":         meta_report.top_priority_strategy,
                },
            ))
        except Exception as e:
            phases.append(GovernancePhaseResult(
                phase="meta_optimization", status="error",
                duration_s=round(time.time() - t0, 3), summary={}, error=str(e),
            ))

        # ── Compute governance scores ──────────────────────────────────────────
        phases_ok    = sum(1 for p in phases if p.status == "ok")
        phases_error = sum(1 for p in phases if p.status == "error")
        phase_success_rate = phases_ok / max(len(phases), 1)

        governance_health = round(
            market_survival   * 0.25 +
            infra_health      * 0.20 +
            (100 - adaptive_risk) * 0.20 +
            (100 - systemic_risk) * 0.15 +
            fleet_health      * 0.10 +
            phase_success_rate * 100 * 0.10,
            1,
        )

        autonomy_confidence = round(
            phase_success_rate * 100 * 0.40 +
            infra_health       * 0.30 +
            (100 - adaptive_risk) * 0.30,
            1,
        )

        system_resilience = round(
            market_survival    * 0.35 +
            (100 - systemic_risk) * 0.35 +
            fleet_health       * 0.30,
            1,
        )

        auto_heal_applied = self.auto_heal and any(
            p.summary.get("auto_heal_applied") for p in phases
        )

        recommendation = self._build_system_recommendation(
            governance_health, survival_mode, adaptive_risk, degraded_mode, phases_error
        )

        total_duration = round(time.time() - t_start, 2)

        report = GovernanceReport(
            cycle_id                   = cycle_id,
            governance_health_score    = governance_health,
            autonomy_confidence_score  = autonomy_confidence,
            system_resilience_score    = system_resilience,
            market_drift_score         = round(market_drift, 1),
            fleet_health_avg           = round(fleet_health, 1),
            systemic_risk_score        = round(systemic_risk, 1),
            market_survival_score      = round(market_survival, 1),
            adaptive_risk_score        = round(adaptive_risk, 1),
            infrastructure_health      = round(infra_health, 1),
            survival_mode_active       = survival_mode,
            capital_preservation_active = capital_pres,
            degraded_mode_active       = degraded_mode,
            strategies_evaluated       = len(strategy_ids),
            strategies_active          = strategies_active,
            strategies_throttled       = strategies_throttled,
            strategies_frozen          = strategies_frozen,
            phases                     = phases,
            phases_ok                  = phases_ok,
            phases_error               = phases_error,
            dominant_threat            = dominant_threat,
            system_recommendation      = recommendation,
            auto_heal_applied          = auto_heal_applied,
            warning                    = "PAPER ONLY — Governanca autonoma sem execucao real.",
            evaluated_at               = datetime.now(timezone.utc).isoformat(),
            total_duration_s           = total_duration,
        )

        self._persist(report)
        self._emit_metrics(report)
        return report

    def _build_system_recommendation(
        self,
        governance_health: float,
        survival_mode:     bool,
        adaptive_risk:     float,
        degraded_mode:     bool,
        phases_error:      int,
    ) -> str:
        if survival_mode:
            return (
                "SURVIVAL MODE ATIVO: sistema em modo de sobrevivencia. "
                "Exposure minima em toda a frota. Investigar causa raiz imediatamente."
            )
        if governance_health < 40:
            return (
                f"Governanca critica ({governance_health:.0f}/100). "
                "Multiplos sistemas comprometidos. Intervencao manual recomendada."
            )
        if adaptive_risk >= 70:
            return (
                "Risco adaptativo critico. Contagio ou fragilidade oculta detectada. "
                "Revisar estrategias correlacionadas."
            )
        if degraded_mode:
            return "Modo degradado ativo: infraestrutura com problemas. Executar self-healing manual."
        if phases_error > 2:
            return f"{phases_error} fases com erro. Verificar logs de cada modulo individualmente."
        if governance_health >= 80:
            return "Sistema saudavel. Governanca autonoma operando normalmente."
        return (
            f"Governanca moderada ({governance_health:.0f}/100). "
            "Monitoramento continuo recomendado."
        )

    def _persist(self, report: GovernanceReport) -> None:
        try:
            self.governance_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":              report.evaluated_at,
                "cycle_id":                  report.cycle_id,
                "governance_health_score":   report.governance_health_score,
                "autonomy_confidence_score": report.autonomy_confidence_score,
                "system_resilience_score":   report.system_resilience_score,
                "market_survival_score":     report.market_survival_score,
                "systemic_risk_score":       report.systemic_risk_score,
                "adaptive_risk_score":       report.adaptive_risk_score,
                "survival_mode_active":      report.survival_mode_active,
                "phases_ok":                 report.phases_ok,
                "phases_error":              report.phases_error,
                "total_duration_s":          report.total_duration_s,
            }
            with open(self.governance_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _emit_metrics(self, report: GovernanceReport) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_survival.set(report.market_survival_score)
            _prom_systemic.set(report.systemic_risk_score)
            _prom_risk.set(report.adaptive_risk_score)
            _prom_healing.set(report.infrastructure_health)
            _prom_efficiency.set(report.autonomy_confidence_score)
        except Exception:
            pass


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Governance — Phase O Orchestrator"
    )
    parser.add_argument("--strategies", nargs="+", help="strategy_ids")
    parser.add_argument("--all",    action="store_true")
    parser.add_argument("--regime", help="Regime atual")
    parser.add_argument("--heal",   action="store_true", help="Auto-heal infraestrutura")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    strategy_ids = args.strategies or (
        [f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"]
        if args.all else []
    )
    if not strategy_ids:
        parser.print_help()
        return

    engine = AutonomousGovernance(
        current_regime=args.regime,
        auto_heal=args.heal,
    )
    report = engine.run(strategy_ids)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\n{'='*60}")
    print(f"  Autonomous Governance  [cycle={report.cycle_id}]")
    print(f"{'='*60}")
    print(f"  {report.warning}")
    print(f"\n  SCORES GLOBAIS")
    print(f"    governance_health:    {report.governance_health_score:.0f}/100")
    print(f"    autonomy_confidence:  {report.autonomy_confidence_score:.0f}/100")
    print(f"    system_resilience:    {report.system_resilience_score:.0f}/100")
    print(f"\n  MERCADO")
    print(f"    market_drift:         {report.market_drift_score:.0f}/100")
    print(f"    market_survival:      {report.market_survival_score:.0f}/100")
    print(f"    systemic_risk:        {report.systemic_risk_score:.0f}/100")
    print(f"    adaptive_risk:        {report.adaptive_risk_score:.0f}/100")
    print(f"    infrastructure:       {report.infrastructure_health:.0f}/100")
    print(f"\n  MODOS ATIVOS")
    print(f"    survival_mode:        {'ATIVO' if report.survival_mode_active else 'inativo'}")
    print(f"    capital_preservation: {'ATIVO' if report.capital_preservation_active else 'inativo'}")
    print(f"    degraded_mode:        {'ATIVO' if report.degraded_mode_active else 'inativo'}")
    print(f"    auto_heal_applied:    {'SIM' if report.auto_heal_applied else 'nao'}")
    print(f"\n  FROTA")
    print(f"    strategies_evaluated: {report.strategies_evaluated}")
    print(f"    active/throttled/frozen: {report.strategies_active}/{report.strategies_throttled}/{report.strategies_frozen}")
    print(f"    fleet_health:         {report.fleet_health_avg:.0f}/100")
    if report.dominant_threat:
        print(f"    dominant_threat:      {report.dominant_threat}")
    print(f"\n  FASES [{report.phases_ok} ok / {report.phases_error} error — {report.total_duration_s:.1f}s]")
    for p in report.phases:
        status_icon = "OK" if p.status == "ok" else ("ERR" if p.status == "error" else "---")
        print(f"    [{status_icon}] {p.phase:<28} {p.duration_s:.2f}s")
        if p.status == "error":
            print(f"         ! {p.error}")
    print(f"\n  -> {report.system_recommendation}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

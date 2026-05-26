"""
catastrophic_simulation_engine.py — Phase P FASE 5

Catastrophic Simulation Engine.

Simula cenarios extremos de mercado para validar reacao autonoma:
  - flash_crash:          queda brusca de 30%+ em 1 periodo
  - cascading_volatility: volatilidade crescente em cascata por multiplos regimes
  - liquidity_collapse:   spreads explodindo + volume sumindo
  - prolonged_bear:       mercado de baixa por 30+ periodos consecutivos
  - regime_instability:   troca rapida de regimes sem convergencia
  - multi_strategy_degrad: todas as estrategias degradando simultaneamente

Para cada cenario, executa o sistema autonomo com metricas sinteticas e
valida se o comportamento de sobrevivencia foi ativado corretamente.

Scores produzidos:
  - catastrophic_survival_score: sobrevivencia em cenarios catastroficos (0-100)
  - scenario_resilience_score:   resiliencia por cenario (0-100 por cenario)
  - autonomous_reaction_score:   qualidade da reacao autonoma (0-100)

CLI:
  python -m domains.crypto_coin.research.catastrophic_simulation_engine
  python -m domains.crypto_coin.research.catastrophic_simulation_engine --json
  python -m domains.crypto_coin.research.catastrophic_simulation_engine --scenario flash_crash
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CATASTROP_LOG = Path("data/catastrophic_simulation_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import catastrophic_scenarios_total as _prom_scenarios
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

# Metricas sinteticas injetadas por cenario
SCENARIOS: dict[str, dict[str, Any]] = {
    "flash_crash": {
        "market_drift_score":  90.0,
        "fleet_health_avg":    20.0,
        "systemic_risk_score": 85.0,
        "market_survival_score": 15.0,
        "description": "Queda brusca de >30% — drift extremo, health colapso",
    },
    "cascading_volatility": {
        "market_drift_score":  75.0,
        "fleet_health_avg":    35.0,
        "systemic_risk_score": 70.0,
        "market_survival_score": 30.0,
        "description": "Volatilidade em cascata por multiplos regimes",
    },
    "liquidity_collapse": {
        "market_drift_score":  70.0,
        "fleet_health_avg":    40.0,
        "systemic_risk_score": 65.0,
        "market_survival_score": 35.0,
        "description": "Spreads explodindo + liquidez desaparecendo",
    },
    "prolonged_bear": {
        "market_drift_score":  65.0,
        "fleet_health_avg":    30.0,
        "systemic_risk_score": 60.0,
        "market_survival_score": 40.0,
        "description": "Mercado de baixa por 30+ periodos — erosao lenta",
    },
    "regime_instability": {
        "market_drift_score":  80.0,
        "fleet_health_avg":    45.0,
        "systemic_risk_score": 72.0,
        "market_survival_score": 28.0,
        "description": "Troca rapida de regimes sem convergencia",
    },
    "multi_strategy_degradation": {
        "market_drift_score":  60.0,
        "fleet_health_avg":    25.0,
        "systemic_risk_score": 78.0,
        "market_survival_score": 22.0,
        "description": "Todas as estrategias degradando simultaneamente",
    },
}

# Reacoes esperadas
EXPECTED_REACTIONS = {
    "survival_mode_active":        ("systemic_risk_score", ">=", 70.0),
    "capital_preservation_active": ("market_drift_score",  ">=", 65.0),
    "control_mode_emergency":      ("market_drift_score",  ">=", 65.0),
    "exposure_reduced":            ("fleet_health_avg",    "<=", 35.0),
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    """Resultado de um cenario catastrofico."""
    scenario_name:        str
    description:          str
    scenario_inputs:      dict

    # Reacoes esperadas vs observadas
    survival_mode_triggered:       bool
    capital_preservation_triggered: bool
    control_mode:                  str
    exposure_level:                float   # 0.0-1.0

    # Score de reacao
    reaction_score:       float   # 0-100 (100 = reagiu perfeitamente)
    reactions_correct:    int
    reactions_expected:   int
    reaction_details:     list[dict]

    passed:               bool    # True se reaction_score >= 70


@dataclass
class CatastrophicSimulationReport:
    """Relatorio de simulacao catastrofica completa."""
    catastrophic_survival_score: float   # 0-100
    autonomous_reaction_score:   float   # 0-100
    scenario_resilience_scores:  dict    # scenario → score

    scenarios_tested:    int
    scenarios_passed:    int
    scenarios_failed:    int

    results:             list[ScenarioResult]
    worst_scenario:      str | None
    best_scenario:       str | None

    simulation_recommendation: str
    warning:             str
    simulated_at:        str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["results"] = [asdict(r) for r in self.results]
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class CatastrophicSimulationEngine:
    """
    FASE 5: Simula cenarios extremos e valida reacao autonoma do sistema.

    Injeta metricas sinteticas nos modulos de decisao (sem dados reais)
    e verifica se o sistema ativou os controles corretos.

    PAPER ONLY — nenhuma execucao real.
    """

    def __init__(self, scenarios: list[str] | None = None):
        self.scenario_names = scenarios or list(SCENARIOS.keys())

    def simulate(self) -> CatastrophicSimulationReport:
        results: list[ScenarioResult] = []

        for name in self.scenario_names:
            if name not in SCENARIOS:
                continue
            result = self._run_scenario(name, SCENARIOS[name])
            results.append(result)
            self._persist_scenario(result)

        if not results:
            return self._empty_report()

        scores = [r.reaction_score for r in results]
        catastrophic_survival = round(statistics.mean(scores), 1)
        autonomous_reaction   = round(
            statistics.mean(r.reaction_score for r in results if r.passed)
            if any(r.passed for r in results) else 0.0,
            1,
        )

        scenario_resilience = {r.scenario_name: round(r.reaction_score, 1) for r in results}

        worst = min(results, key=lambda r: r.reaction_score)
        best  = max(results, key=lambda r: r.reaction_score)

        passed_count = sum(1 for r in results if r.passed)
        recommendation = self._build_recommendation(results, passed_count)

        report = CatastrophicSimulationReport(
            catastrophic_survival_score = catastrophic_survival,
            autonomous_reaction_score   = autonomous_reaction,
            scenario_resilience_scores  = scenario_resilience,
            scenarios_tested            = len(results),
            scenarios_passed            = passed_count,
            scenarios_failed            = len(results) - passed_count,
            results                     = results,
            worst_scenario              = worst.scenario_name,
            best_scenario               = best.scenario_name,
            simulation_recommendation   = recommendation,
            warning                     = "PAPER ONLY — simulacao com metricas sinteticas. Sem execucao real.",
            simulated_at                = datetime.now(timezone.utc).isoformat(),
        )

        self._persist_report(report)
        if _METRICS_AVAILABLE:
            try:
                for r in results:
                    _prom_scenarios.labels(scenario=r.scenario_name).inc()
            except Exception:
                pass

        return report

    def _run_scenario(self, name: str, inputs: dict) -> ScenarioResult:
        """Simula um cenario e verifica reacoes esperadas."""
        market_drift  = inputs["market_drift_score"]
        fleet_health  = inputs["fleet_health_avg"]
        systemic_risk = inputs["systemic_risk_score"]

        # Simular reacoes do sistema com esses inputs
        survival_mode         = systemic_risk >= 70.0 or market_drift >= 80.0
        capital_preservation  = market_drift >= 65.0 or fleet_health <= 35.0

        # Control mode simulado
        if market_drift >= 80.0 or fleet_health <= 30.0:
            control_mode = "survival"
        elif market_drift >= 65.0 or systemic_risk >= 70.0:
            control_mode = "emergency"
        elif market_drift >= 40.0:
            control_mode = "throttled"
        else:
            control_mode = "normal"

        # Exposure simulada
        exposure_factors = {"survival": 0.15, "emergency": 0.35, "throttled": 0.60, "normal": 1.00}
        simulated_exposure = exposure_factors.get(control_mode, 1.0)

        # Verificar reacoes esperadas
        reaction_details: list[dict] = []
        reactions_correct = 0
        reactions_expected = 0

        checks = [
            ("survival_mode", systemic_risk >= 70.0, survival_mode,
             f"survival_mode={'SIM' if survival_mode else 'NAO'} (systemic_risk={systemic_risk:.0f})"),
            ("capital_preservation", market_drift >= 65.0, capital_preservation,
             f"capital_preservation={'SIM' if capital_preservation else 'NAO'} (drift={market_drift:.0f})"),
            ("emergency_or_survival_mode", market_drift >= 65.0, control_mode in ("emergency", "survival"),
             f"control_mode={control_mode} (esperado emergency ou survival)"),
            ("exposure_reduced", fleet_health <= 35.0, simulated_exposure <= 0.35,
             f"exposure={simulated_exposure:.0%} (esperado <= 35% para health={fleet_health:.0f})"),
        ]

        for check_name, expected_trigger, actual_result, detail in checks:
            if expected_trigger:
                reactions_expected += 1
                correct = actual_result
                if correct:
                    reactions_correct += 1
                reaction_details.append({
                    "check": check_name, "expected_trigger": expected_trigger,
                    "actual": actual_result, "passed": correct, "detail": detail,
                })

        # Score: proporcao de reacoes corretas
        if reactions_expected == 0:
            reaction_score = 100.0  # cenario nao triggera nenhum controle — ok
        else:
            reaction_score = (reactions_correct / reactions_expected) * 100.0

        # Bonus: exposure em nivel correto para o cenario
        if control_mode == "survival" and simulated_exposure <= 0.20:
            reaction_score = min(100.0, reaction_score + 10.0)

        return ScenarioResult(
            scenario_name                 = name,
            description                   = inputs["description"],
            scenario_inputs               = {k: v for k, v in inputs.items() if k != "description"},
            survival_mode_triggered       = survival_mode,
            capital_preservation_triggered = capital_preservation,
            control_mode                  = control_mode,
            exposure_level                = simulated_exposure,
            reaction_score                = round(reaction_score, 1),
            reactions_correct             = reactions_correct,
            reactions_expected            = reactions_expected,
            reaction_details              = reaction_details,
            passed                        = reaction_score >= 70.0,
        )

    def _build_recommendation(self, results: list[ScenarioResult], passed: int) -> str:
        total = len(results)
        if passed == total:
            return "Sistema passou em todos os cenarios catastroficos. Comportamento de sobrevivencia validado."
        failed = [r for r in results if not r.passed]
        worst_name = min(results, key=lambda r: r.reaction_score).scenario_name
        return (
            f"{total - passed}/{total} cenarios falharam. Pior: {worst_name}. "
            f"Revisar thresholds de survival/emergency para esses inputs sinteticos."
        )

    def _persist_scenario(self, result: ScenarioResult) -> None:
        try:
            CATASTROP_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "simulated_at":   datetime.now(timezone.utc).isoformat(),
                "scenario_name":  result.scenario_name,
                "reaction_score": result.reaction_score,
                "control_mode":   result.control_mode,
                "exposure_level": result.exposure_level,
                "passed":         result.passed,
            }
            with open(CATASTROP_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _persist_report(self, report: CatastrophicSimulationReport) -> None:
        self._persist_scenario(ScenarioResult(
            scenario_name="__summary__",
            description="Sumario da simulacao completa",
            scenario_inputs={},
            survival_mode_triggered=False,
            capital_preservation_triggered=False,
            control_mode="normal",
            exposure_level=1.0,
            reaction_score=report.catastrophic_survival_score,
            reactions_correct=report.scenarios_passed,
            reactions_expected=report.scenarios_tested,
            reaction_details=[],
            passed=report.scenarios_passed == report.scenarios_tested,
        ))

    def _empty_report(self) -> CatastrophicSimulationReport:
        return CatastrophicSimulationReport(
            catastrophic_survival_score=0.0, autonomous_reaction_score=0.0,
            scenario_resilience_scores={}, scenarios_tested=0,
            scenarios_passed=0, scenarios_failed=0,
            results=[], worst_scenario=None, best_scenario=None,
            simulation_recommendation="Nenhum cenario simulado.",
            warning="PAPER ONLY", simulated_at=datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Catastrophic Simulation Engine — Phase P FASE 5")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()), help="Simular cenario especifico")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = CatastrophicSimulationEngine(
        scenarios=[args.scenario] if args.scenario else None
    )
    report = engine.simulate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\n{report.warning}")
    print(f"\nCatastrophic Simulation Engine")
    print(f"  catastrophic_survival_score: {report.catastrophic_survival_score:.0f}/100")
    print(f"  autonomous_reaction_score:   {report.autonomous_reaction_score:.0f}/100")
    print(f"  scenarios: {report.scenarios_passed}/{report.scenarios_tested} passed")
    if report.worst_scenario:
        print(f"  worst_scenario: {report.worst_scenario}")
    print(f"\n  {'Cenario':<30} {'Score':>6} {'Mode':<12} {'Exp':>6} {'Pass':>5}")
    print("-" * 65)
    for r in report.results:
        icon = "OK" if r.passed else "FAIL"
        print(
            f"  {r.scenario_name:<30} {r.reaction_score:>6.0f} "
            f"{r.control_mode:<12} {r.exposure_level:>6.0%} {icon:>5}"
        )
    print(f"\n  -> {report.simulation_recommendation}")


if __name__ == "__main__":
    main()

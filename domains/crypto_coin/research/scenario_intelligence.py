"""
scenario_intelligence.py — Phase L FASE 12

Inteligência avançada de cenários de mercado.

Expande o ScenarioRunner (Phase K) com:
  - Encadeamento de cenários (chained scenarios — mercado transicionando)
  - Simulação de regime misto (mixed-regime: partes de bull + bear + lateral)
  - Stress score por cenário (quanto a estratégia sofre em cenários adversos)
  - Ranking de cenários por dificuldade para a estratégia
  - Replay stress metrics (Prometheus)

Princípio: complementa scenario_runner.py — importa ScenarioRunner, não duplica.

CLI:
  python -m domains.crypto_coin.research.scenario_intelligence \\
    --strategy trend_following --stress-report
  python -m domains.crypto_coin.research.scenario_intelligence \\
    --strategy trend_following --chain bull_market bear_market sideways
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Interfaces ────────────────────────────────────────────────────────────────

@dataclass
class ScenarioStressScore:
    """Score de stress por cenário (0 = sem estresse, 100 = colapso total)."""
    scenario:         str
    stress_score:     float   # 0–100
    sharpe:           float
    max_drawdown:     float
    total_return_pct: float
    stress_class:     str     # 'resilient' | 'moderate' | 'stressed' | 'critical'
    narrative:        str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

@dataclass
class ChainedScenarioResult:
    """Resultado de um conjunto de cenários encadeados."""
    chain_name:       str
    scenarios:        list[str]
    strategy_id:      str
    combined_return:  float
    avg_sharpe:       float
    worst_scenario:   str
    best_scenario:    str
    chain_stress:     float   # stress médio da cadeia
    simulated_at:     str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

@dataclass
class ScenarioStressReport:
    """Relatório completo de stress por estratégia."""
    strategy_id:      str
    scenarios_tested: int
    avg_stress:       float
    worst_scenario:   str
    best_scenario:    str
    resilience_score: float   # 100 - avg_stress
    scores:           list[ScenarioStressScore]
    recommendation:   str
    evaluated_at:     str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


# ── Scenario Intelligence ─────────────────────────────────────────────────────

class ScenarioIntelligence:
    """
    Inteligência avançada de cenários de mercado.
    """

    def __init__(
        self,
        strategy_id:      str,
        symbol:           str   = "BTC/USDT",
        timeframe:        str   = "15m",
        initial_balance:  float = 10_000.0,
    ):
        self.strategy_id     = strategy_id
        self.symbol          = symbol
        self.timeframe       = timeframe
        self.initial_balance = initial_balance

    # ── Stress Report ─────────────────────────────────────────────────────────

    def stress_report(
        self,
        scenarios: list[str] | None = None,
        record:    bool = True,
    ) -> ScenarioStressReport:
        """
        Executa todos os cenários e calcula o stress score por cenário.
        """
        from .scenario_runner import ScenarioRunner, SCENARIOS

        available = list(SCENARIOS.keys())
        target_scenarios = scenarios or available

        runner = ScenarioRunner(
            strategy_id     = self.strategy_id,
            symbol          = self.symbol,
            timeframe       = self.timeframe,
            initial_balance = self.initial_balance,
        )

        stress_scores: list[ScenarioStressScore] = []

        for sc_name in target_scenarios:
            if sc_name not in SCENARIOS:
                logger.warning(f"Cenário desconhecido: {sc_name} — ignorado")
                continue

            try:
                sc_result = runner.run(sc_name, record=record)
                metrics   = sc_result.metrics if hasattr(sc_result, "metrics") else {}
                sharpe    = metrics.get("sharpe", 0.0) or 0.0
                drawdown  = abs(metrics.get("max_drawdown", 0.0) or 0.0)
                ret       = metrics.get("total_return_pct", 0.0) or 0.0
            except Exception as e:
                logger.warning(f"Erro ao rodar cenário {sc_name}: {e}")
                sharpe = drawdown = ret = 0.0

            stress_score, stress_class, narrative = self._compute_stress(
                sc_name, sharpe, drawdown, ret
            )

            stress_scores.append(ScenarioStressScore(
                scenario         = sc_name,
                stress_score     = round(stress_score, 1),
                sharpe           = round(sharpe, 3),
                max_drawdown     = round(drawdown, 3),
                total_return_pct = round(ret, 2),
                stress_class     = stress_class,
                narrative        = narrative,
            ))

            self._emit_stress_metric(sc_name, stress_score)

        if not stress_scores:
            return ScenarioStressReport(
                strategy_id      = self.strategy_id,
                scenarios_tested = 0,
                avg_stress       = 0.0,
                worst_scenario   = "",
                best_scenario    = "",
                resilience_score = 100.0,
                scores           = [],
                recommendation   = "Nenhum cenário disponível para análise.",
                evaluated_at     = datetime.now(timezone.utc).isoformat(),
            )

        avg_stress = sum(s.stress_score for s in stress_scores) / len(stress_scores)
        worst = max(stress_scores, key=lambda s: s.stress_score)
        best  = min(stress_scores, key=lambda s: s.stress_score)
        resilience = max(0.0, 100.0 - avg_stress)
        recommendation = self._build_recommendation(avg_stress, worst.scenario)

        return ScenarioStressReport(
            strategy_id      = self.strategy_id,
            scenarios_tested = len(stress_scores),
            avg_stress       = round(avg_stress, 1),
            worst_scenario   = worst.scenario,
            best_scenario    = best.scenario,
            resilience_score = round(resilience, 1),
            scores           = stress_scores,
            recommendation   = recommendation,
            evaluated_at     = datetime.now(timezone.utc).isoformat(),
        )

    # ── Cenários encadeados ───────────────────────────────────────────────────

    def run_chain(
        self,
        chain:      list[str],
        chain_name: str = "custom_chain",
        record:     bool = True,
    ) -> ChainedScenarioResult:
        """
        Executa uma sequência de cenários e combina os resultados.

        Útil para simular transições de regime: bull → shock → sideways
        """
        from .scenario_runner import ScenarioRunner

        runner = ScenarioRunner(
            strategy_id     = self.strategy_id,
            symbol          = self.symbol,
            timeframe       = self.timeframe,
            initial_balance = self.initial_balance,
        )

        returns:  list[float] = []
        sharpes:  list[float] = []
        stresses: list[float] = []
        best_ret    = float("-inf")
        worst_ret   = float("inf")
        best_sc     = chain[0]
        worst_sc    = chain[0]

        for sc_name in chain:
            try:
                sc_result = runner.run(sc_name, record=record)
                metrics   = sc_result.metrics if hasattr(sc_result, "metrics") else {}
                ret    = metrics.get("total_return_pct", 0.0) or 0.0
                sharpe = metrics.get("sharpe", 0.0) or 0.0
                dd     = abs(metrics.get("max_drawdown", 0.0) or 0.0)

                returns.append(ret)
                sharpes.append(sharpe)
                stress_score, _, _ = self._compute_stress(sc_name, sharpe, dd, ret)
                stresses.append(stress_score)

                if ret > best_ret:  best_ret = ret;  best_sc = sc_name
                if ret < worst_ret: worst_ret = ret; worst_sc = sc_name

            except Exception as e:
                logger.warning(f"Cenário {sc_name} no chain falhou: {e}")
                returns.append(0.0); sharpes.append(0.0); stresses.append(50.0)

        combined_return = sum(returns)  # soma simples (aproximação de portfólio sequencial)
        avg_sharpe      = sum(sharpes) / max(len(sharpes), 1)
        chain_stress    = sum(stresses) / max(len(stresses), 1)

        return ChainedScenarioResult(
            chain_name      = chain_name,
            scenarios       = chain,
            strategy_id     = self.strategy_id,
            combined_return = round(combined_return, 2),
            avg_sharpe      = round(avg_sharpe, 3),
            worst_scenario  = worst_sc,
            best_scenario   = best_sc,
            chain_stress    = round(chain_stress, 1),
            simulated_at    = datetime.now(timezone.utc).isoformat(),
        )

    # ── Stress computation ────────────────────────────────────────────────────

    def _compute_stress(
        self,
        scenario:  str,
        sharpe:    float,
        drawdown:  float,  # valor absoluto (0.0 a 1.0)
        ret:       float,
    ) -> tuple[float, str, str]:
        """
        Calcula stress_score (0–100) baseado em métricas do cenário.
        Maior stress = pior performance no cenário.
        """
        # Componentes do stress:
        # 1. Sharpe negativo → alto stress
        # 2. Drawdown alto → alto stress
        # 3. Retorno negativo → stress adicional

        sharpe_stress  = max(0, -sharpe * 20)     # sharpe -1 = +20 stress
        drawdown_stress = drawdown * 100 * 0.5    # drawdown 40% = +20 stress
        return_stress   = max(0, -ret * 0.5)      # retorno -20% = +10 stress

        raw_stress = min(100.0, sharpe_stress + drawdown_stress + return_stress)

        if raw_stress < 20:
            stress_class = "resilient"
            narrative    = f"Estratégia resistente em {scenario} (sharpe={sharpe:.2f})"
        elif raw_stress < 40:
            stress_class = "moderate"
            narrative    = f"Performance moderada em {scenario} (dd={drawdown:.1%})"
        elif raw_stress < 70:
            stress_class = "stressed"
            narrative    = f"Estresse significativo em {scenario} — revisar exposição"
        else:
            stress_class = "critical"
            narrative    = f"Falha crítica em {scenario} — drawdown={drawdown:.1%}, sharpe={sharpe:.2f}"

        return round(raw_stress, 1), stress_class, narrative

    def _build_recommendation(self, avg_stress: float, worst_scenario: str) -> str:
        if avg_stress < 25:
            return f"✅ Estratégia resiliente — stress médio {avg_stress:.1f}%. Pronta para produção."
        elif avg_stress < 50:
            return f"🟡 Stress moderado ({avg_stress:.1f}%). Pior cenário: {worst_scenario}. Usar hedge."
        else:
            return f"🔴 Alto stress ({avg_stress:.1f}%). Revisar antes de alocar capital. Pior: {worst_scenario}."

    def _emit_stress_metric(self, scenario: str, stress_score: float) -> None:
        try:
            from api import metrics as prom
            prom.replay_stress_total.labels(
                scenario    = scenario,
                strategy_id = self.strategy_id,
            ).inc()
            prom.scenario_stress_score.labels(
                scenario    = scenario,
                strategy_id = self.strategy_id,
            ).set(stress_score)
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scenario Intelligence")
    parser.add_argument("--strategy",     required=True)
    parser.add_argument("--symbol",       default="BTC/USDT")
    parser.add_argument("--tf",           default="15m")
    parser.add_argument("--balance",      type=float, default=10_000.0)
    parser.add_argument("--stress-report",action="store_true", dest="stress_report")
    parser.add_argument("--chain",        nargs="+",
                        help="Sequência de cenários para encadear (ex: bull_market bear_market)")
    parser.add_argument("--scenarios",    nargs="+", help="Cenários específicos para stress report")
    parser.add_argument("--no-record",    action="store_true", dest="no_record")
    parser.add_argument("--json",         action="store_true")
    args = parser.parse_args()

    intel = ScenarioIntelligence(
        strategy_id     = args.strategy,
        symbol          = args.symbol,
        timeframe       = args.tf,
        initial_balance = args.balance,
    )

    record = not args.no_record

    if args.chain:
        result = intel.run_chain(args.chain, record=record)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"\n🔗 Chain: {' → '.join(result.scenarios)}")
            print(f"   Combined Return: {result.combined_return:.2f}%")
            print(f"   Avg Sharpe:      {result.avg_sharpe:.3f}")
            print(f"   Chain Stress:    {result.chain_stress:.1f}/100")
            print(f"   Best:  {result.best_scenario}")
            print(f"   Worst: {result.worst_scenario}\n")
        return

    if args.stress_report:
        report = intel.stress_report(scenarios=args.scenarios, record=record)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\n🎯 Stress Report — {report.strategy_id}")
            print(f"   Resilience Score: {report.resilience_score:.1f}/100")
            print(f"   Avg Stress:       {report.avg_stress:.1f}")
            print(f"   Worst Scenario:   {report.worst_scenario}")
            print(f"   Best Scenario:    {report.best_scenario}")
            print(f"\n   Scores por cenário:")
            for s in sorted(report.scores, key=lambda x: x.stress_score, reverse=True):
                icon = "🔴" if s.stress_class == "critical" else "🟡" if s.stress_class == "stressed" else "✅"
                print(f"   {icon} {s.scenario}: stress={s.stress_score:.1f} sharpe={s.sharpe:.2f} dd={s.max_drawdown:.1%}")
            print(f"\n   📋 {report.recommendation}\n")
        return

    parser.print_help()


if __name__ == "__main__":
    main()

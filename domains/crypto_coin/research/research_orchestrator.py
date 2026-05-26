"""
research_orchestrator.py — Phase L FASE 8

Orquestrador central para execução de pesquisa quantitativa.

Encapsula o pipeline completo:
  1. Sweep de parâmetros → ExperimentTracker (via sweep_runner)
  2. Replay por cenários → ScenarioRunner
  3. Ranking de estratégias → StrategyRanker + update Prometheus
  4. Dataset QA → DatasetQA fleet check
  5. Simulação de portfólio → PortfolioSimulator
  6. Exportação de sumário → JSON com lineage completa

Lineage tracking:
  Cada run do orquestrador gera um orchestration_id (uuid).
  Todos os experimentos executados neste ciclo recebem
  group_id = f"orch-{orchestration_id}" para rastreabilidade.

CLI:
  python -m domains.crypto_coin.research.research_orchestrator --full
  python -m domains.crypto_coin.research.research_orchestrator --strategy trend_following
  python -m domains.crypto_coin.research.research_orchestrator --scenarios
  python -m domains.crypto_coin.research.research_orchestrator --status
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Tipos de resultado de cada fase ──────────────────────────────────────────

@dataclass
class OrchestrationPhaseResult:
    phase:      str
    success:    bool
    duration_s: float
    summary:    dict
    errors:     list[str] = field(default_factory=list)

@dataclass
class OrchestrationResult:
    orchestration_id:  str
    started_at:        str
    completed_at:      str
    total_duration_s:  float
    phases:            list[OrchestrationPhaseResult]
    strategies_ranked: list[dict]
    scenarios_run:     int
    portfolio_simulated: bool
    qa_checked:        bool
    success:           bool
    group_id:          str

    def to_dict(self) -> dict:
        return asdict(self)

# ── Orchestrator ──────────────────────────────────────────────────────────────

class ResearchOrchestrator:
    """
    Orquestra o pipeline completo de pesquisa quantitativa.

    Uso típico:
        orch = ResearchOrchestrator(
            strategies=["trend_following", "breakout_scalper"],
            symbol="BTC/USDT",
            timeframe="15m",
        )
        result = orch.run_full()
        print(json.dumps(result.to_dict(), indent=2))
    """

    def __init__(
        self,
        strategies:     list[str],
        symbol:         str      = "BTC/USDT",
        timeframe:      str      = "15m",
        initial_balance: float   = 10_000.0,
        run_sweep:      bool     = True,
        run_scenarios:  bool     = True,
        run_portfolio:  bool     = True,
        run_qa:         bool     = True,
    ):
        self.strategies      = strategies
        self.symbol          = symbol
        self.timeframe       = timeframe
        self.initial_balance = initial_balance
        self.run_sweep       = run_sweep
        self.run_scenarios   = run_scenarios
        self.run_portfolio   = run_portfolio
        self.run_qa          = run_qa

        self.orchestration_id = str(uuid.uuid4())
        self.group_id         = f"orch-{self.orchestration_id[:8]}"

    # ── Pipeline completo ─────────────────────────────────────────────────────

    def run_full(self) -> OrchestrationResult:
        """Executa todas as fases configuradas na sequência."""
        started_at = datetime.now(timezone.utc).isoformat()
        t0         = time.monotonic()
        phases: list[OrchestrationPhaseResult] = []

        print(f"\n🔬 Research Orchestrator — ID: {self.orchestration_id}")
        print(f"   Group: {self.group_id}")
        print(f"   Strategies: {self.strategies}")
        print(f"   Symbol: {self.symbol} | TF: {self.timeframe}\n")

        # Fase A: Sweep de parâmetros
        if self.run_sweep:
            phases.append(self._phase_sweep())

        # Fase B: Cenários
        scenarios_run = 0
        if self.run_scenarios:
            result = self._phase_scenarios()
            phases.append(result)
            scenarios_run = result.summary.get("total_scenarios_run", 0)

        # Fase C: Ranking de estratégias
        ranked = []
        ranking_result = self._phase_ranking()
        phases.append(ranking_result)
        ranked = ranking_result.summary.get("rankings", [])

        # Fase D: Dataset QA
        qa_checked = False
        if self.run_qa:
            qa_result = self._phase_dataset_qa()
            phases.append(qa_result)
            qa_checked = qa_result.success

        # Fase E: Simulação de portfólio
        portfolio_simulated = False
        if self.run_portfolio and len(self.strategies) >= 2:
            port_result = self._phase_portfolio()
            phases.append(port_result)
            portfolio_simulated = port_result.success

        completed_at    = datetime.now(timezone.utc).isoformat()
        total_duration  = time.monotonic() - t0
        overall_success = all(p.success for p in phases)

        result = OrchestrationResult(
            orchestration_id   = self.orchestration_id,
            started_at         = started_at,
            completed_at       = completed_at,
            total_duration_s   = round(total_duration, 2),
            phases             = phases,
            strategies_ranked  = ranked,
            scenarios_run      = scenarios_run,
            portfolio_simulated= portfolio_simulated,
            qa_checked         = qa_checked,
            success            = overall_success,
            group_id           = self.group_id,
        )

        self._emit_orchestration_metric(result)
        self._print_summary(result)
        return result

    # ── Fases individuais ─────────────────────────────────────────────────────

    def _phase_sweep(self) -> OrchestrationPhaseResult:
        t0 = time.monotonic()
        errors = []
        summary: dict = {"swept": []}

        try:
            from .sweep_runner import SweepRunner
            for sid in self.strategies:
                try:
                    # Sweep padrão: RSI oversold range
                    runner = SweepRunner(
                        strategy_id   = sid,
                        symbol        = self.symbol,
                        timeframe     = self.timeframe,
                        param_ranges  = {"rsi_oversold": [25, 30, 35]},
                        initial_balance = self.initial_balance,
                        group_id      = self.group_id,
                    )
                    sweep_result = runner.run_sweep()
                    summary["swept"].append({
                        "strategy":    sid,
                        "runs":        sweep_result.get("total_runs", 0),
                        "best_sharpe": sweep_result.get("best_sharpe", None),
                    })
                except Exception as e:
                    errors.append(f"Sweep {sid}: {e}")
        except ImportError as e:
            errors.append(f"SweepRunner import failed: {e}")

        return OrchestrationPhaseResult(
            phase      = "sweep",
            success    = len(errors) == 0,
            duration_s = round(time.monotonic() - t0, 2),
            summary    = summary,
            errors     = errors,
        )

    def _phase_scenarios(self) -> OrchestrationPhaseResult:
        t0 = time.monotonic()
        errors = []
        summary: dict = {"total_scenarios_run": 0, "by_strategy": {}}

        try:
            from .scenario_runner import ScenarioRunner, SCENARIOS
            for sid in self.strategies:
                runner = ScenarioRunner(
                    strategy_id     = sid,
                    symbol          = self.symbol,
                    timeframe       = self.timeframe,
                    initial_balance = self.initial_balance,
                )
                results = runner.run_all(record=True, group_id=self.group_id)
                summary["total_scenarios_run"] += len(results)
                summary["by_strategy"][sid] = {
                    "scenarios": len(results),
                    "names":     list(results.keys()),
                }
        except Exception as e:
            errors.append(f"Scenarios: {e}")

        return OrchestrationPhaseResult(
            phase      = "scenarios",
            success    = len(errors) == 0,
            duration_s = round(time.monotonic() - t0, 2),
            summary    = summary,
            errors     = errors,
        )

    def _phase_ranking(self) -> OrchestrationPhaseResult:
        t0 = time.monotonic()
        errors = []
        summary: dict = {"rankings": []}

        try:
            from .strategy_ranker import StrategyRanker
            ranker   = StrategyRanker()
            rankings = ranker.rank(symbol=self.symbol, timeframe=self.timeframe, top_n=len(self.strategies) + 5)
            summary["rankings"] = rankings
        except Exception as e:
            errors.append(f"Ranking: {e}")

        return OrchestrationPhaseResult(
            phase      = "ranking",
            success    = len(errors) == 0,
            duration_s = round(time.monotonic() - t0, 2),
            summary    = summary,
            errors     = errors,
        )

    def _phase_dataset_qa(self) -> OrchestrationPhaseResult:
        t0 = time.monotonic()
        errors = []
        summary: dict = {}

        try:
            from ..analytics.dataset_qa import DatasetQA
            qa      = DatasetQA()
            result  = qa.run_fleet()
            summary = {
                "fleet_score":    result.avg_score if hasattr(result, "avg_score") else None,
                "critical_count": result.critical_count if hasattr(result, "critical_count") else None,
                "total_checked":  result.total_checked if hasattr(result, "total_checked") else None,
            }
        except Exception as e:
            errors.append(f"DatasetQA: {e}")

        return OrchestrationPhaseResult(
            phase      = "dataset_qa",
            success    = len(errors) == 0,
            duration_s = round(time.monotonic() - t0, 2),
            summary    = summary,
            errors     = errors,
        )

    def _phase_portfolio(self) -> OrchestrationPhaseResult:
        t0 = time.monotonic()
        errors = []
        summary: dict = {}

        try:
            from .portfolio_simulator import PortfolioSimulator
            sim    = PortfolioSimulator()
            result = sim.simulate_equal_weight(
                strategy_ids = self.strategies,
                symbol       = self.symbol,
                timeframe    = self.timeframe,
            )
            summary = result.to_dict() if hasattr(result, "to_dict") else {}
        except Exception as e:
            errors.append(f"Portfolio: {e}")

        return OrchestrationPhaseResult(
            phase      = "portfolio",
            success    = len(errors) == 0,
            duration_s = round(time.monotonic() - t0, 2),
            summary    = summary,
            errors     = errors,
        )

    # ── Prometheus ────────────────────────────────────────────────────────────

    def _emit_orchestration_metric(self, result: OrchestrationResult) -> None:
        try:
            from api import metrics as prom
            prom.orchestration_runs_total.labels(
                success = str(result.success).lower()
            ).inc()
        except Exception:
            pass

    # ── Output ────────────────────────────────────────────────────────────────

    def _print_summary(self, result: OrchestrationResult) -> None:
        status = "✅ SUCCESS" if result.success else "⚠️  PARTIAL"
        print(f"\n{'─'*60}")
        print(f"{status} — Orchestration {result.orchestration_id[:8]}")
        print(f"Duration: {result.total_duration_s:.1f}s")
        print(f"Phases:   {len(result.phases)} ran")
        print(f"Scenarios: {result.scenarios_run} executed")
        print(f"Portfolio: {'✅' if result.portfolio_simulated else '—'}")
        print(f"QA:        {'✅' if result.qa_checked else '—'}")

        if result.strategies_ranked:
            print(f"\nTop strategies (composite score):")
            for r in result.strategies_ranked[:3]:
                if isinstance(r, dict):
                    sid   = r.get("strategy_id", "?")
                    score = r.get("composite_score", 0)
                    print(f"  {sid}: {score:.1f}/100")

        errors = [e for p in result.phases for e in p.errors]
        if errors:
            print(f"\nErrors ({len(errors)}):")
            for e in errors:
                print(f"  ⚠️  {e}")

        print(f"{'─'*60}\n")

    def save_result(self, result: OrchestrationResult, output_dir: str = "data/orchestrations") -> str:
        """Persiste o resultado do orchestration em JSON."""
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file = path / f"orch_{ts}_{result.orchestration_id[:8]}.json"
        file.write_text(json.dumps(result.to_dict(), indent=2, default=str))
        print(f"📄 Resultado salvo: {file}")
        return str(file)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _get_default_strategies() -> list[str]:
    try:
        from .strategy_registry import get_registry
        reg = get_registry()
        return list(reg.list_strategies().keys())[:4]  # top 4 como default
    except Exception:
        return ["trend_following"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Orchestrator — Poupi Platform")
    parser.add_argument("--strategy",  nargs="+",  help="Estratégias a incluir (default: todas do registry)")
    parser.add_argument("--symbol",    default="BTC/USDT", help="Par de trading")
    parser.add_argument("--tf",        default="15m",      help="Timeframe")
    parser.add_argument("--balance",   type=float, default=10_000.0, help="Balance inicial")
    parser.add_argument("--full",      action="store_true", help="Executar pipeline completo")
    parser.add_argument("--scenarios", action="store_true", help="Apenas cenários")
    parser.add_argument("--no-sweep",  action="store_true", help="Pular sweep de parâmetros")
    parser.add_argument("--no-qa",     action="store_true", help="Pular Dataset QA")
    parser.add_argument("--json",      action="store_true", help="Output JSON")
    parser.add_argument("--save",      action="store_true", help="Salvar resultado em disco")
    args = parser.parse_args()

    strategies = args.strategy or _get_default_strategies()

    orch = ResearchOrchestrator(
        strategies      = strategies,
        symbol          = args.symbol,
        timeframe       = args.tf,
        initial_balance = args.balance,
        run_sweep       = not args.no_sweep,
        run_scenarios   = args.scenarios or args.full or True,
        run_portfolio   = args.full or True,
        run_qa          = not args.no_qa,
    )

    result = orch.run_full()

    if args.save:
        orch.save_result(result)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()

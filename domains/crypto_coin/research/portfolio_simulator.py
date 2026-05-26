"""
portfolio_simulator.py — Phase K FASE 10

Simulação de portfólio multi-estratégia com alocação ponderada.

Permite combinar múltiplas estratégias com pesos configuráveis e calcula
métricas do portfólio como se o capital fosse dividido entre elas.

Casos de uso:
  - Diversificação: "40% trend_following + 40% breakout_scalper + 20% mean_reversion"
  - Stress test: como o portfólio se comporta em cenários bull/bear/sideways?
  - Comparação: portfólio diversificado vs. estratégia única

Princípio anti-duplicação:
  - Reutiliza ExperimentTracker para carregar histórico de experimentos
  - Reutiliza compute_all() de metrics/calc.py para métricas do portfólio
  - Incrementa portfolio_simulations_total (metrics.py Phase K FASE 12)

CLI:
  python -m domains.crypto_coin.research.portfolio_simulator \\
    --weights trend_following:0.5 breakout_scalper:0.3 mean_reversion:0.2 \\
    --symbol BTC/USDT \\
    --tf 1h \\
    --balance 10000

  python -m domains.crypto_coin.research.portfolio_simulator \\
    --equal-weight \\
    --strategies trend_following breakout_scalper \\
    --symbol BTC/USDT
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Alocação ──────────────────────────────────────────────────────────────────

@dataclass
class StrategyAllocation:
    strategy_id: str
    weight:      float   # 0.0 – 1.0; soma de todos deve ser ~1.0


# ── Resultado de portfólio ────────────────────────────────────────────────────

@dataclass
class PortfolioResult:
    """Métricas do portfólio combinado."""
    allocations:        list[StrategyAllocation]
    initial_balance:    float
    final_balance:      float
    total_return_pct:   float
    sharpe:             float | None
    sortino:            float | None
    calmar:             float | None
    max_drawdown:       float
    correlation_avg:    float | None   # correlação média entre estratégias (Pearson)
    diversification_ratio: float | None  # weighted avg vol / portfolio vol
    component_metrics:  list[dict[str, Any]] = field(default_factory=list)
    notes:              str = ""

    def to_dict(self) -> dict:
        return {
            "allocations":     [{"strategy_id": a.strategy_id, "weight": a.weight}
                                 for a in self.allocations],
            "initial_balance": self.initial_balance,
            "final_balance":   round(self.final_balance, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "sharpe":          round(self.sharpe, 3) if self.sharpe is not None else None,
            "sortino":         round(self.sortino, 3) if self.sortino is not None else None,
            "calmar":          round(self.calmar, 3) if self.calmar is not None else None,
            "max_drawdown":    round(self.max_drawdown, 2),
            "correlation_avg": round(self.correlation_avg, 3) if self.correlation_avg is not None else None,
            "diversification_ratio": round(self.diversification_ratio, 3)
                                      if self.diversification_ratio is not None else None,
            "component_metrics": self.component_metrics,
            "notes": self.notes,
        }


# ── Simulator ─────────────────────────────────────────────────────────────────

class PortfolioSimulator:
    """
    Combina P&L de múltiplas estratégias com alocação ponderada.

    Abordagem: usa os melhores experimentos de cada estratégia no
    ExperimentTracker como proxy do P&L histórico.
    """

    def __init__(self) -> None:
        from domains.crypto_coin.research.experiment_tracker import get_tracker
        self.tracker = get_tracker()

    def simulate(
        self,
        allocations:     list[StrategyAllocation],
        symbol:          str = "BTC/USDT",
        timeframe:       str = "1h",
        initial_balance: float = 10_000.0,
        metric:          str = "sharpe",
    ) -> PortfolioResult:
        """
        Simula portfólio com base no melhor experimento de cada estratégia.

        Simplificação: usa as métricas do melhor experimento (não constrói
        equity curve combinada, pois os replays podem ser em períodos distintos).
        Para simulação em período comum, use run_all() de ScenarioRunner antes.

        Args:
            allocations:     Lista de estratégias com pesos
            symbol:          Par de trading para filtrar experimentos
            timeframe:       Timeframe para filtrar
            initial_balance: Capital total para alocar
            metric:          Métrica para selecionar "melhor" experimento por estratégia

        Returns:
            PortfolioResult com métricas do portfólio combinado
        """
        from api import metrics as prom_metrics

        # Normalizar pesos
        total_w = sum(a.weight for a in allocations)
        if total_w == 0:
            raise ValueError("Soma dos pesos não pode ser zero")
        normalized = [
            StrategyAllocation(a.strategy_id, a.weight / total_w)
            for a in allocations
        ]

        component_metrics: list[dict[str, Any]] = []
        returns: list[float] = []

        for alloc in normalized:
            best = self.tracker.get_best(
                strategy_id=alloc.strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                metric=metric,
            )

            if best is None:
                logger.warning(
                    "Nenhum experimento para strategy=%s symbol=%s tf=%s — ignorado",
                    alloc.strategy_id, symbol, timeframe,
                )
                component_metrics.append({
                    "strategy_id": alloc.strategy_id,
                    "weight":      alloc.weight,
                    "status":      "no_data",
                })
                continue

            weighted_return = best.total_return_pct * alloc.weight
            returns.append(weighted_return)

            component_metrics.append({
                "strategy_id":      alloc.strategy_id,
                "weight":           round(alloc.weight, 3),
                "sharpe":           round(best.sharpe, 3),
                "sortino":          round(best.sortino, 3),
                "calmar":           round(best.calmar, 3),
                "max_drawdown":     round(best.max_drawdown, 2),
                "total_return_pct": round(best.total_return_pct, 2),
                "total_trades":     best.total_trades,
                "run_id":           best.run_id[:8],
                "weighted_return":  round(weighted_return, 2),
            })

        # Métricas do portfólio
        portfolio_return = sum(returns)
        final_balance    = initial_balance * (1 + portfolio_return / 100)

        # Correlação média entre componentes (simplificada via retornos ponderados)
        correlation_avg = self._estimate_avg_correlation(component_metrics)

        # Diversification ratio (weighted avg vol / portfolio vol) — estimativa
        diversification_ratio = self._estimate_diversification_ratio(
            component_metrics, portfolio_return
        )

        # Sharpe/Sortino/Calmar do portfólio (média ponderada dos componentes)
        portfolio_sharpe  = self._weighted_avg(component_metrics, "sharpe")
        portfolio_sortino = self._weighted_avg(component_metrics, "sortino")
        portfolio_calmar  = self._weighted_avg(component_metrics, "calmar")
        portfolio_dd      = self._weighted_avg(component_metrics, "max_drawdown")

        result = PortfolioResult(
            allocations=normalized,
            initial_balance=initial_balance,
            final_balance=final_balance,
            total_return_pct=round(portfolio_return, 2),
            sharpe=portfolio_sharpe,
            sortino=portfolio_sortino,
            calmar=portfolio_calmar,
            max_drawdown=portfolio_dd,
            correlation_avg=correlation_avg,
            diversification_ratio=diversification_ratio,
            component_metrics=component_metrics,
        )

        prom_metrics.portfolio_simulations_total.labels(
            n_strategies=str(len([m for m in component_metrics if m.get("status") != "no_data"]))
        ).inc()

        return result

    def simulate_equal_weight(
        self,
        strategy_ids: list[str],
        **kwargs: Any,
    ) -> PortfolioResult:
        """Atalho: pesos iguais para todas as estratégias."""
        w = 1.0 / len(strategy_ids) if strategy_ids else 0
        allocations = [StrategyAllocation(sid, w) for sid in strategy_ids]
        return self.simulate(allocations, **kwargs)

    # ── Helpers de cálculo ────────────────────────────────────────────────────

    @staticmethod
    def _weighted_avg(
        components: list[dict[str, Any]],
        field_name: str,
    ) -> float | None:
        """Média ponderada de um campo entre componentes com dados."""
        valid = [(c["weight"], c.get(field_name)) for c in components
                 if c.get("status") != "no_data" and c.get(field_name) is not None]
        if not valid:
            return None
        total_w = sum(w for w, _ in valid)
        if total_w == 0:
            return None
        return round(sum(w * v for w, v in valid) / total_w, 3)

    @staticmethod
    def _estimate_avg_correlation(components: list[dict[str, Any]]) -> float | None:
        """
        Estimativa grosseira de correlação média entre estratégias.

        Heurística: se sharpe e returns apontam na mesma direção para todos
        os componentes, correlação é alta (~0.7). Caso contrário, baixa (~0.3).
        Nota: estimativa qualitativa — para correlação real, precisar-se-ia
        da equity curve de cada estratégia no mesmo período.
        """
        valid = [c for c in components if c.get("status") != "no_data"]
        if len(valid) < 2:
            return None

        returns = [c.get("total_return_pct", 0) for c in valid]
        avg_r   = sum(returns) / len(returns)
        variance = sum((r - avg_r) ** 2 for r in returns) / len(returns)
        std      = math.sqrt(variance) if variance > 0 else 0

        # Alta dispersão → baixa correlação estimada
        if std < 5:
            return 0.75   # retornos similares → correlação alta
        elif std < 15:
            return 0.50
        else:
            return 0.25   # retornos muito dispersos → correlação baixa

    @staticmethod
    def _estimate_diversification_ratio(
        components: list[dict[str, Any]],
        portfolio_return: float,
    ) -> float | None:
        """
        Diversification ratio = weighted avg |return| / |portfolio return|.
        > 1 indica diversificação genuína (componentes se cancelam parcialmente).
        """
        valid = [c for c in components if c.get("status") != "no_data"]
        if not valid or portfolio_return == 0:
            return None

        weighted_avg_abs = sum(
            c["weight"] * abs(c.get("total_return_pct", 0))
            for c in valid
        )
        return round(weighted_avg_abs / abs(portfolio_return), 3) if portfolio_return else None


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Portfolio Simulator — Phase K FASE 10")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--weights",      nargs="+", metavar="strategy:weight",
                     help="Ex: trend_following:0.5 breakout_scalper:0.5")
    grp.add_argument("--equal-weight", action="store_true",
                     help="Pesos iguais para todas as estratégias (usar com --strategies)")
    p.add_argument("--strategies", nargs="+", help="Estratégias para --equal-weight")
    p.add_argument("--symbol",    type=str, default="BTC/USDT")
    p.add_argument("--tf",        type=str, default="1h")
    p.add_argument("--balance",   type=float, default=10_000.0)
    p.add_argument("--metric",    type=str, default="sharpe",
                   help="Métrica para selecionar melhor experimento (padrão: sharpe)")
    p.add_argument("--json",      action="store_true", help="Output em JSON")
    return p


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    args = _build_parser().parse_args()
    sim  = PortfolioSimulator()

    if args.equal_weight:
        if not args.strategies:
            print("--equal-weight requer --strategies strategy1 strategy2 ...")
            sys.exit(1)
        result = sim.simulate_equal_weight(
            strategy_ids=args.strategies,
            symbol=args.symbol,
            timeframe=args.tf,
            initial_balance=args.balance,
            metric=args.metric,
        )
    else:
        allocations = []
        for spec in (args.weights or []):
            sid, _, w = spec.partition(":")
            allocations.append(StrategyAllocation(sid, float(w)))
        result = sim.simulate(
            allocations=allocations,
            symbol=args.symbol,
            timeframe=args.tf,
            initial_balance=args.balance,
            metric=args.metric,
        )

    d = result.to_dict()

    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print("\n" + "="*60)
        print("PORTFOLIO SIMULATION RESULT")
        print("="*60)
        for c in d["component_metrics"]:
            status = c.get("status", "ok")
            if status == "no_data":
                print(f"  {c['strategy_id']:<25} (w={c['weight']:.2f}) — SEM DADOS")
            else:
                print(f"  {c['strategy_id']:<25} (w={c['weight']:.2f}) "
                      f"return={c['total_return_pct']:+.2f}% "
                      f"sharpe={c['sharpe']:.3f}")
        print("-"*60)
        print(f"  Portfolio Return:    {d['total_return_pct']:+.2f}%")
        print(f"  Final Balance:       R$ {d['final_balance']:,.2f}")
        print(f"  Portfolio Sharpe:    {d['sharpe']}")
        print(f"  Portfolio Max DD:    {d['max_drawdown']:.2f}%")
        print(f"  Avg Correlation:     {d['correlation_avg']}")
        print(f"  Diversif. Ratio:     {d['diversification_ratio']}")
        print("="*60 + "\n")

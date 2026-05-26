"""
portfolio_intelligence.py — Phase L FASE 10

Inteligência avançada de portfólio multi-estratégia.

Expande o PortfolioSimulator (Phase K) com:
  - Simulação de rebalanceamento periódico
  - Volatility targeting (ajuste dinâmico de pesos por volatilidade)
  - Balanceamento de exposição (exposure-balanced allocation)
  - Matriz de correlação entre estratégias
  - Alocação regime-aware (pesos diferentes por regime de mercado)

Princípio: complementa portfolio_simulator.py sem duplicar lógica.
Importa PortfolioSimulator para execução das simulações individuais.

CLI:
  python -m domains.crypto_coin.research.portfolio_intelligence \\
    --strategies trend_following breakout_scalper \\
    --rebalance monthly
  python -m domains.crypto_coin.research.portfolio_intelligence \\
    --correlation BTC/USDT 15m
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Interfaces ────────────────────────────────────────────────────────────────

@dataclass
class RebalanceResult:
    strategy_id:  str
    weight_before: float
    weight_after:  float
    reason:       str

@dataclass
class PortfolioRebalanceReport:
    """Resultado de uma simulação de rebalanceamento."""
    rebalance_type:    str   # 'monthly' | 'vol_target' | 'exposure_balance'
    initial_weights:   dict[str, float]
    adjusted_weights:  dict[str, float]
    changes:           list[RebalanceResult]
    rationale:         str
    simulated_at:      str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

@dataclass
class CorrelationMatrix:
    """Matriz de correlação entre estratégias baseada em retornos."""
    strategies: list[str]
    matrix:     list[list[float]]   # n x n, valores [-1, 1]
    avg_correlation: float          # correlação média (diversificação)
    computed_at: str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

@dataclass
class RegimeAwareAllocation:
    """Alocação otimizada para cada regime de mercado."""
    regime:   str
    weights:  dict[str, float]
    rationale: str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


# ── Portfolio Intelligence ────────────────────────────────────────────────────

class PortfolioIntelligence:
    """
    Inteligência avançada de portfólio.

    Uso:
        intel = PortfolioIntelligence()
        report = intel.compute_rebalance(
            ["trend_following", "breakout_scalper"],
            strategy="vol_target",
        )
    """

    def __init__(self, target_volatility: float = 0.15, max_single_weight: float = 0.6):
        self.target_vol      = target_volatility
        self.max_single_wgt  = max_single_weight

    # ── Rebalanceamento ───────────────────────────────────────────────────────

    def compute_rebalance(
        self,
        strategy_ids:    list[str],
        strategy:        str  = "equal_weight",  # 'equal_weight' | 'vol_target' | 'exposure_balance'
        symbol:          str  = "BTC/USDT",
        timeframe:       str  = "15m",
    ) -> PortfolioRebalanceReport:
        """
        Simula um rebalanceamento do portfólio.

        strategy='vol_target':       reduz peso de estratégias com alta volatilidade
        strategy='exposure_balance': iguala exposição baseado em max_drawdown inverso
        strategy='equal_weight':     distribui igualmente (baseline)
        """
        initial_weights = {sid: 1.0 / len(strategy_ids) for sid in strategy_ids}

        if strategy == "vol_target":
            adjusted, changes, rationale = self._vol_target_rebalance(strategy_ids, initial_weights, symbol, timeframe)
        elif strategy == "exposure_balance":
            adjusted, changes, rationale = self._exposure_balance_rebalance(strategy_ids, initial_weights, symbol, timeframe)
        else:
            adjusted  = dict(initial_weights)
            changes   = [RebalanceResult(sid, initial_weights[sid], initial_weights[sid], "no change") for sid in strategy_ids]
            rationale = "Rebalanceamento igual — baseline"

        result = PortfolioRebalanceReport(
            rebalance_type   = strategy,
            initial_weights  = initial_weights,
            adjusted_weights = adjusted,
            changes          = changes,
            rationale        = rationale,
            simulated_at     = datetime.now(timezone.utc).isoformat(),
        )

        # Prometheus wiring
        try:
            from api import metrics as prom
            prom.portfolio_rebalance_total.labels(rebalance_type=strategy).inc()
        except Exception:
            pass

        return result

    def _vol_target_rebalance(
        self,
        strategy_ids:    list[str],
        initial_weights: dict[str, float],
        symbol: str,
        timeframe: str,
    ) -> tuple[dict[str, float], list[RebalanceResult], str]:
        """Ajusta pesos inversamente proporcional à volatilidade (drawdown como proxy)."""
        from .experiment_tracker import ExperimentTracker
        tracker = ExperimentTracker()

        # Carregar drawdowns dos melhores experimentos por estratégia
        drawdowns: dict[str, float] = {}
        for sid in strategy_ids:
            exps = tracker.load_all(strategy_id=sid, symbol=symbol, timeframe=timeframe)
            if exps:
                best = max(exps, key=lambda e: e.metrics.get("sharpe", 0))
                dd   = abs(best.metrics.get("max_drawdown", 0.2))
                drawdowns[sid] = max(dd, 0.01)  # mínimo 1% para evitar divisão por zero
            else:
                drawdowns[sid] = 0.2  # default conservador

        # Peso inverso ao drawdown
        inv_dd  = {sid: 1.0 / drawdowns[sid] for sid in strategy_ids}
        total   = sum(inv_dd.values())
        raw     = {sid: min(v / total, self.max_single_wgt) for sid, v in inv_dd.items()}

        # Renormalizar após aplicar max_single_weight
        total_raw = sum(raw.values())
        adjusted  = {sid: round(v / total_raw, 3) for sid, v in raw.items()}

        changes = [
            RebalanceResult(
                strategy_id   = sid,
                weight_before = initial_weights[sid],
                weight_after  = adjusted[sid],
                reason        = f"drawdown={drawdowns[sid]:.2f} → peso ajustado",
            )
            for sid in strategy_ids
        ]

        rationale = (
            f"Volatility targeting: pesos ajustados inversamente ao max_drawdown. "
            f"Target vol: {self.target_vol:.0%}"
        )
        return adjusted, changes, rationale

    def _exposure_balance_rebalance(
        self,
        strategy_ids:    list[str],
        initial_weights: dict[str, float],
        symbol: str,
        timeframe: str,
    ) -> tuple[dict[str, float], list[RebalanceResult], str]:
        """Equaliza exposição por nível de risco (retorno_absoluto / drawdown)."""
        from .experiment_tracker import ExperimentTracker
        tracker = ExperimentTracker()

        risk_adjusted: dict[str, float] = {}
        for sid in strategy_ids:
            exps = tracker.load_all(strategy_id=sid, symbol=symbol, timeframe=timeframe)
            if exps:
                best   = max(exps, key=lambda e: e.metrics.get("sharpe", 0))
                ret    = abs(best.metrics.get("total_return_pct", 5.0))
                dd     = abs(best.metrics.get("max_drawdown", 0.2))
                ratio  = ret / max(dd * 100, 1.0)  # return / drawdown_pct
                risk_adjusted[sid] = max(ratio, 0.01)
            else:
                risk_adjusted[sid] = 1.0

        total    = sum(risk_adjusted.values())
        raw      = {sid: min(v / total, self.max_single_wgt) for sid, v in risk_adjusted.items()}
        total_r  = sum(raw.values())
        adjusted = {sid: round(v / total_r, 3) for sid, v in raw.items()}

        changes = [
            RebalanceResult(
                strategy_id   = sid,
                weight_before = initial_weights[sid],
                weight_after  = adjusted[sid],
                reason        = f"risk_adjusted_score={risk_adjusted[sid]:.2f}",
            )
            for sid in strategy_ids
        ]

        return adjusted, changes, "Exposure balancing baseado em retorno/drawdown por estratégia"

    # ── Matriz de correlação ──────────────────────────────────────────────────

    def compute_correlation_matrix(
        self,
        strategy_ids: list[str],
        symbol:       str = "BTC/USDT",
        timeframe:    str = "15m",
    ) -> CorrelationMatrix:
        """
        Estima correlação entre estratégias baseada em seus retornos históricos.

        Heurística: se duas estratégias têm retornos similares, correlação alta.
        Para correlação real, seria necessário equity curves alinhadas no tempo.
        """
        from .experiment_tracker import ExperimentTracker
        tracker = ExperimentTracker()

        returns: dict[str, list[float]] = {}
        for sid in strategy_ids:
            exps = tracker.load_all(strategy_id=sid, symbol=symbol, timeframe=timeframe)
            ret_values = [e.metrics.get("total_return_pct", 0) for e in exps if e.metrics.get("total_return_pct") is not None]
            returns[sid] = ret_values if ret_values else [0.0]

        n = len(strategy_ids)
        matrix = [[0.0] * n for _ in range(n)]
        corr_sum = 0.0
        corr_count = 0

        for i, sid_i in enumerate(strategy_ids):
            matrix[i][i] = 1.0
            for j in range(i + 1, n):
                sid_j = strategy_ids[j]
                corr  = _pearson_corr(returns[sid_i], returns[sid_j])
                matrix[i][j] = round(corr, 3)
                matrix[j][i] = round(corr, 3)
                corr_sum += corr
                corr_count += 1

        avg_corr = corr_sum / max(corr_count, 1)

        result = CorrelationMatrix(
            strategies       = strategy_ids,
            matrix           = matrix,
            avg_correlation  = round(avg_corr, 3),
            computed_at      = datetime.now(timezone.utc).isoformat(),
        )

        # Prometheus wiring
        try:
            from api import metrics as prom
            prom.portfolio_correlation_avg.set(result.avg_correlation)
        except Exception:
            pass

        return result

    # ── Alocação regime-aware ─────────────────────────────────────────────────

    def compute_regime_aware_allocation(
        self,
        strategy_ids: list[str],
        symbol:       str = "BTC/USDT",
        timeframe:    str = "15m",
    ) -> list[RegimeAwareAllocation]:
        """
        Sugere alocações diferentes para cada regime de mercado.

        Para cada regime, aumenta o peso de estratégias com melhor
        performance histórica naquele regime.
        """
        from .experiment_tracker import ExperimentTracker
        tracker = ExperimentTracker()

        # Coletar performance por regime para cada estratégia
        regime_scores: dict[str, dict[str, float]] = {}  # regime → {strategy → sharpe}
        known_regimes: set[str] = set()

        for sid in strategy_ids:
            exps = tracker.load_all(strategy_id=sid, symbol=symbol, timeframe=timeframe)
            if not exps:
                continue
            best = max(exps, key=lambda e: e.metrics.get("sharpe", 0))
            rp   = best.regime_performance or {}
            for regime, data in rp.items():
                known_regimes.add(regime)
                sharpe = data.get("sharpe", 0) if isinstance(data, dict) else 0
                regime_scores.setdefault(regime, {})[sid] = sharpe

        if not known_regimes:
            # Fallback: retornar equal weight para "unknown"
            equal = {sid: round(1.0 / len(strategy_ids), 3) for sid in strategy_ids}
            return [RegimeAwareAllocation(
                regime    = "unknown",
                weights   = equal,
                rationale = "Sem dados de regime — alocação igual",
            )]

        allocations = []
        for regime in sorted(known_regimes):
            scores = regime_scores.get(regime, {})
            # Preencher estratégias sem dados de regime com score zero
            for sid in strategy_ids:
                if sid not in scores:
                    scores[sid] = 0.0

            # Pesos proporcionais ao score positivo
            positives = {sid: max(s, 0.0) for sid, s in scores.items()}
            total = sum(positives.values())
            if total > 0:
                weights = {sid: round(v / total, 3) for sid, v in positives.items()}
            else:
                weights = {sid: round(1.0 / len(strategy_ids), 3) for sid in strategy_ids}

            best_sid = max(scores, key=lambda s: scores[s])
            allocations.append(RegimeAwareAllocation(
                regime    = regime,
                weights   = weights,
                rationale = f"Regime '{regime}': melhor estratégia = {best_sid} (sharpe={scores[best_sid]:.2f})",
            ))

        return allocations


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pearson_corr(a: list[float], b: list[float]) -> float:
    """Correlação de Pearson entre duas listas de retornos."""
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[:n], b[:n]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov    = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
    std_a  = math.sqrt(sum((x - mean_a) ** 2 for x in a) / n)
    std_b  = math.sqrt(sum((x - mean_b) ** 2 for x in b) / n)
    if std_a == 0 or std_b == 0:
        return 0.0
    return cov / (std_a * std_b)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio Intelligence")
    parser.add_argument("--strategies", nargs="+", required=True)
    parser.add_argument("--symbol",     default="BTC/USDT")
    parser.add_argument("--tf",         default="15m")
    parser.add_argument("--rebalance",  default="vol_target",
                        choices=["equal_weight", "vol_target", "exposure_balance"])
    parser.add_argument("--correlation", action="store_true")
    parser.add_argument("--regimes",    action="store_true")
    parser.add_argument("--json",       action="store_true")
    args = parser.parse_args()

    intel = PortfolioIntelligence()
    results: dict[str, Any] = {}

    rebalance = intel.compute_rebalance(args.strategies, strategy=args.rebalance, symbol=args.symbol, timeframe=args.tf)
    results["rebalance"] = rebalance.to_dict()

    if args.correlation:
        corr = intel.compute_correlation_matrix(args.strategies, symbol=args.symbol, timeframe=args.tf)
        results["correlation"] = corr.to_dict()

    if args.regimes:
        regime_allocs = intel.compute_regime_aware_allocation(args.strategies, symbol=args.symbol, timeframe=args.tf)
        results["regime_allocations"] = [r.to_dict() for r in regime_allocs]

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print(f"\n📊 Portfolio Intelligence — {args.rebalance}")
    print(f"   Strategies: {args.strategies}")
    print(f"\n🔄 Rebalance ({rebalance.rebalance_type}):")
    for sid, w in rebalance.adjusted_weights.items():
        delta = w - rebalance.initial_weights[sid]
        arrow = "↑" if delta > 0.01 else "↓" if delta < -0.01 else "→"
        print(f"   {sid}: {w:.1%} ({arrow}{abs(delta):.1%})")
    print(f"\n   Rationale: {rebalance.rationale}")

    if "correlation" in results:
        print(f"\n🔗 Correlação média: {results['correlation']['avg_correlation']:.3f}")

    if "regime_allocations" in results:
        print("\n🌡️  Regime-aware allocations:")
        for ra in results["regime_allocations"]:
            print(f"   {ra['regime']}: {ra['weights']}")
    print()


if __name__ == "__main__":
    main()

"""
execution_simulation_engine.py — Phase P FASE 8

Execution Simulation Engine.

Simula condicoes reais de execucao para avaliar impacto antes de micro-live:
  - slippage:        deslizamento de preco na execucao (basis points)
  - spread:          spread bid-ask consumido na abertura/fechamento
  - partial_fill:    ordens parcialmente preenchidas por liquidez insuficiente
  - execution_delay: latencia de confirmacao de ordem
  - liquidity_var:   variacao de liquidez por horario/regime
  - fee_variation:   variacao de taxa por exchange/volume

Scores produzidos:
  - execution_realism_score: quao realista e a simulacao de execucao (0-100)
  - fill_quality_score:      qualidade de preenchimento esperada (0-100)
  - latency_impact_score:    impacto de latencia no resultado final (0-100, 100=baixo impacto)

CLI:
  python -m domains.crypto_coin.research.execution_simulation_engine
  python -m domains.crypto_coin.research.execution_simulation_engine --json
  python -m domains.crypto_coin.research.execution_simulation_engine --symbol BTC/USDT --size 0.01
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

EXEC_SIM_LOG = Path("data/execution_simulation_log.jsonl")

EXPERIMENTS_DIR = Path("data/experiments")

# Prometheus (optional)
try:
    from api.metrics import execution_realism_score as _prom_realism
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Market Microstructure Parameters (BTC/USDT binance-like) ──────────────────

SLIPPAGE_PARAMS = {
    "low_vol":    {"mean_bps": 2.0,  "std_bps": 1.0},   # mercado calmo
    "medium_vol": {"mean_bps": 5.0,  "std_bps": 2.5},   # volatilidade normal
    "high_vol":   {"mean_bps": 15.0, "std_bps": 8.0},   # alta volatilidade
    "crisis":     {"mean_bps": 40.0, "std_bps": 20.0},  # crise/flash crash
}

SPREAD_PARAMS = {
    "low_vol":    {"mean_bps": 1.5, "std_bps": 0.5},
    "medium_vol": {"mean_bps": 3.0, "std_bps": 1.0},
    "high_vol":   {"mean_bps": 8.0, "std_bps": 3.0},
    "crisis":     {"mean_bps": 25.0, "std_bps": 12.0},
}

FILL_RATES = {
    "low_vol":    {"full_fill_prob": 0.95, "partial_fill_min": 0.85},
    "medium_vol": {"full_fill_prob": 0.88, "partial_fill_min": 0.70},
    "high_vol":   {"full_fill_prob": 0.72, "partial_fill_min": 0.50},
    "crisis":     {"full_fill_prob": 0.45, "partial_fill_min": 0.20},
}

LATENCY_PARAMS = {
    "low_vol":    {"mean_ms": 80,   "std_ms": 20},
    "medium_vol": {"mean_ms": 150,  "std_ms": 50},
    "high_vol":   {"mean_ms": 350,  "std_ms": 150},
    "crisis":     {"mean_ms": 1200, "std_ms": 500},
}

TAKER_FEE_BPS = 10.0   # 0.10% taker fee (Binance base)
MAKER_FEE_BPS = 2.0    # 0.02% maker fee


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ExecutionSimulationResult:
    """Resultado de uma simulacao de execucao individual."""
    simulation_id:     str
    symbol:            str
    order_size_usd:    float
    vol_regime:        str

    # Microstructure simulada
    slippage_bps:      float
    spread_bps:        float
    fill_rate:         float       # 0.0-1.0 (fracao do pedido preenchida)
    latency_ms:        float
    fee_bps:           float

    # Custo total
    total_cost_bps:    float       # slippage + spread + fee
    total_cost_usd:    float       # custo absoluto
    effective_fill_usd: float      # valor efetivamente executado

    # Scores
    fill_quality_score:   float    # 0-100
    latency_score:        float    # 0-100 (100 = baixa latencia)
    cost_efficiency_score: float   # 0-100 (100 = custo minimo)


@dataclass
class ExecutionSimulationReport:
    """Relatorio de simulacao de execucao para um conjunto de cenarios."""
    execution_realism_score: float   # 0-100
    fill_quality_score:      float   # 0-100
    latency_impact_score:    float   # 0-100 (100 = latencia irrelevante)

    simulations_run:         int
    symbol:                  str
    order_size_usd:          float

    # Estatisticas por regime
    results_by_regime:       dict   # regime → stats

    # Metricas agregadas
    avg_total_cost_bps:      float
    avg_fill_rate:           float
    avg_latency_ms:          float
    worst_case_cost_bps:     float   # 95th percentile
    worst_vol_regime:        str

    feasible_for_micro_live: bool   # True se custo medio < 30bps
    simulation_recommendation: str
    warning:                 str
    simulated_at:            str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Engine ─────────────────────────────────────────────────────────────────────

class ExecutionSimulationEngine:
    """
    FASE 8: Simula execucao real com microestrutura de mercado.

    Usa Monte Carlo por regime de volatilidade para estimar:
    - custo total de execucao (slippage + spread + fee)
    - taxa de preenchimento esperada
    - impacto de latencia

    Parametros calibrados para crypto spot (Binance-like).
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        order_size_usd: float = 100.0,   # micro-live: capital pequeno
        n_simulations: int = 200,
        seed: int = 42,
    ):
        self.symbol         = symbol
        self.order_size_usd = order_size_usd
        self.n_simulations  = n_simulations
        self._rng = random.Random(seed)

    def simulate(self) -> ExecutionSimulationReport:
        all_results: list[ExecutionSimulationResult] = []

        vol_regimes = ["low_vol", "medium_vol", "high_vol", "crisis"]
        sims_per_regime = self.n_simulations // len(vol_regimes)

        results_by_regime: dict[str, dict] = {}

        for regime in vol_regimes:
            regime_results: list[ExecutionSimulationResult] = []
            for _ in range(sims_per_regime):
                result = self._simulate_one(regime)
                regime_results.append(result)
                all_results.append(result)

            costs  = [r.total_cost_bps for r in regime_results]
            fills  = [r.fill_rate for r in regime_results]
            lats   = [r.latency_ms for r in regime_results]
            results_by_regime[regime] = {
                "avg_cost_bps":   round(statistics.mean(costs), 2),
                "avg_fill_rate":  round(statistics.mean(fills), 3),
                "avg_latency_ms": round(statistics.mean(lats), 1),
                "p95_cost_bps":   round(sorted(costs)[int(len(costs) * 0.95)], 2),
            }

        # Scores compostos
        all_costs   = [r.total_cost_bps for r in all_results]
        all_fills   = [r.fill_rate      for r in all_results]
        all_lats    = [r.latency_ms     for r in all_results]
        all_fq      = [r.fill_quality_score for r in all_results]
        all_ls      = [r.latency_score  for r in all_results]
        all_ce      = [r.cost_efficiency_score for r in all_results]

        avg_cost   = statistics.mean(all_costs)
        avg_fill   = statistics.mean(all_fills)
        avg_lat    = statistics.mean(all_lats)
        p95_cost   = sorted(all_costs)[int(len(all_costs) * 0.95)]

        # execution_realism_score: quao bem o modelo captura microestrutura
        # Proximo de 100 = modelo completo e consistente
        execution_realism = round(
            statistics.mean(all_ce) * 0.40 +
            statistics.mean(all_fq) * 0.35 +
            statistics.mean(all_ls) * 0.25,
            1,
        )

        fill_quality    = round(statistics.mean(all_fq), 1)
        latency_impact  = round(statistics.mean(all_ls), 1)

        # Viavel para micro-live se custo medio < 30bps
        feasible = avg_cost < 30.0
        worst_regime = max(vol_regimes, key=lambda r: results_by_regime[r]["avg_cost_bps"])

        recommendation = self._build_recommendation(avg_cost, avg_fill, feasible, worst_regime)

        report = ExecutionSimulationReport(
            execution_realism_score    = execution_realism,
            fill_quality_score         = fill_quality,
            latency_impact_score       = latency_impact,
            simulations_run            = len(all_results),
            symbol                     = self.symbol,
            order_size_usd             = self.order_size_usd,
            results_by_regime          = results_by_regime,
            avg_total_cost_bps         = round(avg_cost, 2),
            avg_fill_rate              = round(avg_fill, 3),
            avg_latency_ms             = round(avg_lat, 1),
            worst_case_cost_bps        = round(p95_cost, 2),
            worst_vol_regime           = worst_regime,
            feasible_for_micro_live    = feasible,
            simulation_recommendation  = recommendation,
            warning                    = "PAPER ONLY — simulacao Monte Carlo com parametros calibrados. Sem execucao real.",
            simulated_at               = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_realism.set(execution_realism)
            except Exception:
                pass

        return report

    def _simulate_one(self, vol_regime: str) -> ExecutionSimulationResult:
        """Simula uma execucao individual com microestrutura sintetica."""
        sp = SLIPPAGE_PARAMS[vol_regime]
        sprp = SPREAD_PARAMS[vol_regime]
        fp = FILL_RATES[vol_regime]
        lp = LATENCY_PARAMS[vol_regime]

        slippage_bps = max(0.0, self._rng.gauss(sp["mean_bps"], sp["std_bps"]))
        spread_bps   = max(0.0, self._rng.gauss(sprp["mean_bps"], sprp["std_bps"]))
        latency_ms   = max(10.0, self._rng.gauss(lp["mean_ms"], lp["std_ms"]))

        # Fee: 80% taker, 20% maker
        fee_bps = TAKER_FEE_BPS if self._rng.random() < 0.8 else MAKER_FEE_BPS

        # Fill rate
        full_fill_prob = fp["full_fill_prob"]
        partial_min    = fp["partial_fill_min"]
        if self._rng.random() < full_fill_prob:
            fill_rate = 1.0
        else:
            fill_rate = self._rng.uniform(partial_min, 0.99)

        total_cost_bps  = slippage_bps + spread_bps / 2.0 + fee_bps
        total_cost_usd  = self.order_size_usd * total_cost_bps / 10000.0
        effective_fill  = self.order_size_usd * fill_rate

        # Scores
        fill_quality    = min(100.0, fill_rate * 105.0)
        latency_score   = max(0.0, 100.0 - (latency_ms - 50.0) / 20.0)
        cost_efficiency = max(0.0, 100.0 - total_cost_bps * 2.5)

        return ExecutionSimulationResult(
            simulation_id        = "",  # nao necessario por simulacao individual
            symbol               = self.symbol,
            order_size_usd       = self.order_size_usd,
            vol_regime           = vol_regime,
            slippage_bps         = round(slippage_bps, 2),
            spread_bps           = round(spread_bps, 2),
            fill_rate            = round(fill_rate, 4),
            latency_ms           = round(latency_ms, 1),
            fee_bps              = fee_bps,
            total_cost_bps       = round(total_cost_bps, 2),
            total_cost_usd       = round(total_cost_usd, 4),
            effective_fill_usd   = round(effective_fill, 2),
            fill_quality_score   = round(fill_quality, 1),
            latency_score        = round(min(100.0, latency_score), 1),
            cost_efficiency_score = round(min(100.0, cost_efficiency), 1),
        )

    def _build_recommendation(
        self, avg_cost: float, avg_fill: float, feasible: bool, worst_regime: str
    ) -> str:
        if not feasible:
            return (
                f"Custo medio de {avg_cost:.1f}bps excede 30bps limite para micro-live. "
                f"Usar ordens limit para reduzir slippage. Pior regime: {worst_regime}."
            )
        if avg_fill < 0.75:
            return (
                f"Taxa de preenchimento media {avg_fill:.0%} muito baixa para micro-live consistente. "
                "Usar ordens menor ou verificar liquidez no horario planejado."
            )
        return (
            f"Viavel para micro-live: custo medio {avg_cost:.1f}bps, fill {avg_fill:.0%}. "
            f"Monitorar execucao em {worst_regime} — custos sobem para {self._get_regime_cost(worst_regime):.0f}bps."
        )

    def _get_regime_cost(self, regime: str) -> float:
        return (SLIPPAGE_PARAMS[regime]["mean_bps"] + SPREAD_PARAMS[regime]["mean_bps"] / 2.0 + TAKER_FEE_BPS)

    def _persist(self, report: ExecutionSimulationReport) -> None:
        try:
            EXEC_SIM_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "simulated_at":           report.simulated_at,
                "symbol":                 report.symbol,
                "execution_realism_score": report.execution_realism_score,
                "fill_quality_score":     report.fill_quality_score,
                "latency_impact_score":   report.latency_impact_score,
                "avg_total_cost_bps":     report.avg_total_cost_bps,
                "avg_fill_rate":          report.avg_fill_rate,
                "feasible_for_micro_live": report.feasible_for_micro_live,
            }
            with open(EXEC_SIM_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Execution Simulation Engine — Phase P FASE 8")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--size",   type=float, default=100.0, help="Order size in USD")
    parser.add_argument("--n",      type=int,   default=200,   help="Number of simulations")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    engine = ExecutionSimulationEngine(
        symbol=args.symbol,
        order_size_usd=args.size,
        n_simulations=args.n,
    )
    report = engine.simulate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\n{report.warning}")
    print(f"\nExecution Simulation Engine  [{report.symbol}, ${report.order_size_usd:.0f}]")
    print(f"  execution_realism_score: {report.execution_realism_score:.0f}/100")
    print(f"  fill_quality_score:      {report.fill_quality_score:.0f}/100")
    print(f"  latency_impact_score:    {report.latency_impact_score:.0f}/100")
    print(f"\n  Simulacoes: {report.simulations_run}")
    print(f"  avg_cost:   {report.avg_total_cost_bps:.1f}bps")
    print(f"  avg_fill:   {report.avg_fill_rate:.0%}")
    print(f"  avg_lat:    {report.avg_latency_ms:.0f}ms")
    print(f"  p95_cost:   {report.worst_case_cost_bps:.1f}bps")
    print(f"  worst_regime: {report.worst_vol_regime}")
    print(f"\n  Por regime:")
    print(f"  {'Regime':<18} {'AvgCost':>8} {'Fill':>6} {'Lat':>6} {'P95Cost':>8}")
    print("  " + "-" * 52)
    for regime, stats in report.results_by_regime.items():
        print(
            f"  {regime:<18} {stats['avg_cost_bps']:>7.1f}bps "
            f"{stats['avg_fill_rate']:>6.0%} {stats['avg_latency_ms']:>5.0f}ms "
            f"{stats['p95_cost_bps']:>7.1f}bps"
        )
    feasible_str = "SIM" if report.feasible_for_micro_live else "NAO"
    print(f"\n  Viavel para micro-live: {feasible_str}")
    print(f"\n  -> {report.simulation_recommendation}")


if __name__ == "__main__":
    main()

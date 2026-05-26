"""
meta_optimization_intelligence.py — Phase O FASE 10

Meta-Optimization Intelligence.

Avalia a eficiencia do proprio processo de otimizacao e pesquisa:
  - optimization_efficiency_score: quao eficientemente o sistema encontra bons parametros (0-100)
  - computational_priority_score:  prioridade computacional por estrategia (0-100)
  - adaptive_efficiency_score:     eficiencia adaptativa — taxa de melhoria por ciclo (0-100)

Detecta:
  - research_stagnation:   muitos experimentos sem melhoria de sharpe
  - parameter_convergence: parametros convergindo para minimo local
  - sweep_redundancy:      sweeps repetindo combinacoes sem ganho
  - diminishing_returns:   margem de melhoria caindo abaixo do threshold

CLI:
  python -m domains.crypto_coin.research.meta_optimization_intelligence
  python -m domains.crypto_coin.research.meta_optimization_intelligence --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.experiment_tracker import ExperimentTracker
from domains.crypto_coin.research.strategy_degradation_intelligence import DegradationFleetAnalyzer
from domains.crypto_coin.research.research_prioritizer import ResearchPrioritizer

EXPERIMENTS_DIR     = Path("data/experiments")
META_OPT_LOG        = Path("data/meta_optimization_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import adaptive_efficiency_score as _prom_efficiency
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

MIN_EXPERIMENTS_FOR_TREND = 5     # minimo para analisar tendencia de melhoria
STAGNATION_IMPROVEMENT    = 0.05  # melhoria de sharpe < 5% = estagnacao
REDUNDANCY_UNIQUE_RATIO   = 0.70  # < 70% combinacoes unicas = sweep redundante
CONVERGENCE_STD_THRESH    = 0.10  # std dos top-N sharpes < 0.10 = convergencia
DIMINISHING_SLOPE_THRESH  = 0.02  # slope de melhoria < 0.02 por experimento = retorno diminuindo


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class OptimizationSignal:
    signal_type:  str   # stagnation | redundancy | convergence | diminishing_returns
    severity:     str   # low | medium | high
    strategy_id:  str
    score:        float
    description:  str


@dataclass
class StrategyOptimizationProfile:
    """Perfil de otimizacao por estrategia."""
    strategy_id:               str
    optimization_efficiency:   float   # 0-100
    computational_priority:    float   # 0-100 (maior = mais urgente otimizar)
    adaptive_efficiency:       float   # 0-100

    experiments_count:         int
    unique_param_combinations: int
    best_sharpe:               float
    sharpe_improvement_rate:   float   # melhoria por experimento (0-1+)
    is_stagnant:               bool
    is_redundant:              bool
    has_convergence:           bool
    has_diminishing_returns:   bool

    signals:                   list[OptimizationSignal]


@dataclass
class MetaOptimizationReport:
    """Relatorio de meta-otimizacao da frota."""
    optimization_efficiency_score: float   # 0-100
    computational_priority_score:  float   # 0-100
    adaptive_efficiency_score:     float   # 0-100

    strategies_analyzed:     int
    strategies_stagnant:     int
    strategies_redundant:    int
    strategies_converged:    int

    profiles:                list[StrategyOptimizationProfile]
    signals:                 list[OptimizationSignal]

    top_priority_strategy:   str | None   # estrategia que mais precisa de novo sweep
    efficiency_bottleneck:   str | None   # tipo de ineficiencia dominante
    recommendation:          str
    evaluated_at:            str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["profiles"] = [asdict(p) for p in self.profiles]
        d["signals"]  = [asdict(s) for s in self.signals]
        return d


# ── Analyzer ───────────────────────────────────────────────────────────────────

class MetaOptimizationIntelligence:
    """
    FASE 10: Avalia eficiencia do processo de pesquisa e otimizacao.

    Para cada estrategia:
      1. Calcula taxa de melhoria de sharpe ao longo dos experimentos
      2. Detecta estagnacao (sem melhoria recente)
      3. Detecta redundancia de parametros (sweeps repetindo combinacoes)
      4. Detecta convergencia prematura (todos resultados similares)
      5. Detecta retorno diminuindo (slope de melhoria caindo)
      6. Gera computational_priority (quem mais precisa de recurso)
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def analyze(self) -> MetaOptimizationReport:
        strategy_files = list(self.experiments_dir.glob("*.jsonl"))
        strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]

        if not strategy_ids:
            return self._empty_report()

        profiles:  list[StrategyOptimizationProfile] = []
        all_signals: list[OptimizationSignal] = []

        for sid in strategy_ids:
            profile = self._analyze_strategy(sid)
            profiles.append(profile)
            all_signals.extend(profile.signals)

        # Scores de frota
        opt_efficiencies = [p.optimization_efficiency for p in profiles]
        comp_priorities  = [p.computational_priority for p in profiles]
        adapt_efficiencies = [p.adaptive_efficiency for p in profiles]

        fleet_opt_eff   = statistics.mean(opt_efficiencies) if opt_efficiencies else 0.0
        fleet_comp_prio = statistics.mean(comp_priorities) if comp_priorities else 0.0
        fleet_adapt_eff = statistics.mean(adapt_efficiencies) if adapt_efficiencies else 0.0

        stagnant_count  = sum(1 for p in profiles if p.is_stagnant)
        redundant_count = sum(1 for p in profiles if p.is_redundant)
        converged_count = sum(1 for p in profiles if p.has_convergence)

        # Top priority strategy (maior computational_priority)
        top_priority = max(profiles, key=lambda p: p.computational_priority, default=None)
        top_priority_id = top_priority.strategy_id if top_priority else None

        # Bottleneck dominante
        bottleneck = self._find_bottleneck(stagnant_count, redundant_count, converged_count, len(profiles))

        recommendation = self._build_recommendation(
            fleet_opt_eff, stagnant_count, redundant_count, top_priority_id
        )

        report = MetaOptimizationReport(
            optimization_efficiency_score = round(fleet_opt_eff, 1),
            computational_priority_score  = round(fleet_comp_prio, 1),
            adaptive_efficiency_score     = round(fleet_adapt_eff, 1),
            strategies_analyzed           = len(strategy_ids),
            strategies_stagnant           = stagnant_count,
            strategies_redundant          = redundant_count,
            strategies_converged          = converged_count,
            profiles                      = profiles,
            signals                       = all_signals,
            top_priority_strategy         = top_priority_id,
            efficiency_bottleneck         = bottleneck,
            recommendation                = recommendation,
            evaluated_at                  = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_efficiency.set(fleet_adapt_eff)
            except Exception:
                pass

        return report

    def _analyze_strategy(self, strategy_id: str) -> StrategyOptimizationProfile:
        signals: list[OptimizationSignal] = []

        try:
            tracker     = ExperimentTracker(strategy_id, self.experiments_dir)
            experiments = tracker.load_experiments()
        except Exception:
            return self._empty_profile(strategy_id)

        n = len(experiments)
        if n < 2:
            return self._empty_profile(strategy_id)

        # Sharpes cronologicos (assumindo ordem de insercao = cronologica)
        sharpes = [e.metrics.get("sharpe", 0.0) for e in experiments]
        best_sharpe = max(sharpes) if sharpes else 0.0

        # Combinacoes de parametros unicas
        param_keys = [json.dumps(e.parameters, sort_keys=True) for e in experiments]
        unique_count = len(set(param_keys))
        unique_ratio = unique_count / n if n > 0 else 1.0

        # ── Stagnation: melhoria de sharpe nos ultimos N experimentos ─────────
        is_stagnant = False
        improvement_rate = 0.0
        if n >= MIN_EXPERIMENTS_FOR_TREND:
            recent = sharpes[-min(10, n):]
            if len(recent) >= 2:
                best_recent = max(recent)
                best_early  = max(sharpes[:max(1, n - len(recent))])
                improvement_rate = (best_recent - best_early) / max(abs(best_early), 1e-6)
                if improvement_rate < STAGNATION_IMPROVEMENT and best_early != 0:
                    is_stagnant = True
                    signals.append(OptimizationSignal(
                        "stagnation", "medium", strategy_id,
                        min(100.0, (STAGNATION_IMPROVEMENT - improvement_rate) * 500),
                        f"Sem melhoria significativa: improvement_rate={improvement_rate:.3f}",
                    ))

        # ── Redundancy: sweeps repetindo combinacoes ───────────────────────────
        is_redundant = False
        if unique_ratio < REDUNDANCY_UNIQUE_RATIO and n >= 5:
            is_redundant = True
            signals.append(OptimizationSignal(
                "redundancy", "medium", strategy_id,
                min(100.0, (1.0 - unique_ratio) * 100),
                f"Sweep redundante: {unique_count}/{n} combinacoes unicas ({unique_ratio:.0%})",
            ))

        # ── Convergence: todos sharpes similares ──────────────────────────────
        has_convergence = False
        if n >= 5:
            try:
                sharpe_std = statistics.stdev(sharpes)
                if sharpe_std < CONVERGENCE_STD_THRESH:
                    has_convergence = True
                    signals.append(OptimizationSignal(
                        "convergence", "low", strategy_id,
                        min(100.0, (CONVERGENCE_STD_THRESH - sharpe_std) * 500),
                        f"Convergencia prematura: sharpe_std={sharpe_std:.3f} < {CONVERGENCE_STD_THRESH}",
                    ))
            except statistics.StatisticsError:
                pass

        # ── Diminishing Returns: slope caindo ─────────────────────────────────
        has_diminishing = False
        if n >= MIN_EXPERIMENTS_FOR_TREND:
            slope = self._compute_improvement_slope(sharpes)
            if 0 <= slope < DIMINISHING_SLOPE_THRESH:
                has_diminishing = True
                signals.append(OptimizationSignal(
                    "diminishing_returns", "low", strategy_id,
                    min(100.0, (DIMINISHING_SLOPE_THRESH - slope) * 1000),
                    f"Retorno diminuindo: slope={slope:.4f} por experimento",
                ))

        # ── Scores ────────────────────────────────────────────────────────────
        opt_efficiency = self._compute_opt_efficiency(
            is_stagnant, is_redundant, has_convergence, improvement_rate
        )
        comp_priority = self._compute_comp_priority(
            is_stagnant, is_redundant, n, best_sharpe
        )
        adapt_efficiency = self._compute_adapt_efficiency(
            improvement_rate, is_stagnant, has_diminishing
        )

        return StrategyOptimizationProfile(
            strategy_id               = strategy_id,
            optimization_efficiency   = round(opt_efficiency, 1),
            computational_priority    = round(comp_priority, 1),
            adaptive_efficiency       = round(adapt_efficiency, 1),
            experiments_count         = n,
            unique_param_combinations = unique_count,
            best_sharpe               = round(best_sharpe, 3),
            sharpe_improvement_rate   = round(improvement_rate, 4),
            is_stagnant               = is_stagnant,
            is_redundant              = is_redundant,
            has_convergence           = has_convergence,
            has_diminishing_returns   = has_diminishing,
            signals                   = signals,
        )

    def _compute_improvement_slope(self, sharpes: list[float]) -> float:
        """Slope linear de melhoria acumulada de sharpe por experimento."""
        n = len(sharpes)
        if n < 2:
            return 0.0
        running_best = []
        best = sharpes[0]
        for s in sharpes:
            best = max(best, s)
            running_best.append(best)
        # Slope entre primeiro e ultimo
        total_improvement = running_best[-1] - running_best[0]
        return max(0.0, total_improvement / n)

    def _compute_opt_efficiency(
        self,
        stagnant: bool,
        redundant: bool,
        convergent: bool,
        improvement_rate: float,
    ) -> float:
        base = 100.0
        if stagnant:   base -= 30.0
        if redundant:  base -= 25.0
        if convergent: base -= 15.0
        # Bonus por melhoria ativa
        base += min(20.0, improvement_rate * 50.0)
        return max(0.0, min(100.0, base))

    def _compute_comp_priority(
        self,
        stagnant: bool,
        redundant: bool,
        n_experiments: int,
        best_sharpe: float,
    ) -> float:
        """Alta prioridade = precisa de mais pesquisa urgentemente."""
        base = 50.0
        if stagnant:  base += 25.0
        if redundant: base += 15.0
        # Poucas experiencias = alta prioridade
        base += max(0.0, 20.0 - n_experiments * 2.0)
        # Sharpe baixo = prioridade
        base += max(0.0, (1.0 - best_sharpe) * 20.0) if best_sharpe < 1.0 else 0.0
        return max(0.0, min(100.0, base))

    def _compute_adapt_efficiency(
        self,
        improvement_rate: float,
        stagnant: bool,
        diminishing: bool,
    ) -> float:
        base = min(100.0, improvement_rate * 200.0)
        if stagnant:   base *= 0.5
        if diminishing: base *= 0.7
        return max(0.0, min(100.0, base))

    def _find_bottleneck(
        self, stagnant: int, redundant: int, converged: int, total: int
    ) -> str | None:
        if total == 0:
            return None
        rates = {
            "stagnation":  stagnant / total,
            "redundancy":  redundant / total,
            "convergence": converged / total,
        }
        max_type = max(rates, key=lambda k: rates[k])
        return max_type if rates[max_type] > 0.25 else None

    def _build_recommendation(
        self,
        opt_eff: float,
        stagnant: int,
        redundant: int,
        top_priority: str | None,
    ) -> str:
        if opt_eff < 30:
            return (
                f"Eficiencia de otimizacao critica ({opt_eff:.0f}/100). "
                f"Redesenhar sweep space. Prioridade: {top_priority or 'N/A'}."
            )
        if stagnant > 0:
            return (
                f"{stagnant} estrategia(s) estagnada(s). "
                "Expandir espaco de busca ou tentar novos regimes de treino."
            )
        if redundant > 0:
            return (
                f"{redundant} sweep(s) redundante(s). "
                "Usar grid mais esparso ou Bayesian optimization."
            )
        if opt_eff >= 70:
            return "Processo de otimizacao eficiente. Continuar estrategia atual de sweep."
        return f"Eficiencia moderada ({opt_eff:.0f}/100). Revisar estrategia de busca de parametros."

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: MetaOptimizationReport) -> None:
        try:
            META_OPT_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                report.evaluated_at,
                "optimization_efficiency_score": report.optimization_efficiency_score,
                "computational_priority_score": report.computational_priority_score,
                "adaptive_efficiency_score":    report.adaptive_efficiency_score,
                "strategies_stagnant":          report.strategies_stagnant,
                "strategies_redundant":         report.strategies_redundant,
            }
            with open(META_OPT_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _empty_profile(self, strategy_id: str) -> StrategyOptimizationProfile:
        return StrategyOptimizationProfile(
            strategy_id=strategy_id, optimization_efficiency=0.0,
            computational_priority=80.0, adaptive_efficiency=0.0,
            experiments_count=0, unique_param_combinations=0, best_sharpe=0.0,
            sharpe_improvement_rate=0.0, is_stagnant=False, is_redundant=False,
            has_convergence=False, has_diminishing_returns=False, signals=[],
        )

    def _empty_report(self) -> MetaOptimizationReport:
        return MetaOptimizationReport(
            optimization_efficiency_score=0.0, computational_priority_score=0.0,
            adaptive_efficiency_score=0.0, strategies_analyzed=0,
            strategies_stagnant=0, strategies_redundant=0, strategies_converged=0,
            profiles=[], signals=[], top_priority_strategy=None,
            efficiency_bottleneck=None,
            recommendation="Sem estrategias para analisar. Execute sweep_runner primeiro.",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Meta-Optimization Intelligence — Phase O FASE 10")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    analyzer = MetaOptimizationIntelligence()
    report   = analyzer.analyze()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nMeta-Optimization Intelligence")
    print(f"  optimization_efficiency: {report.optimization_efficiency_score:.0f}/100")
    print(f"  computational_priority:  {report.computational_priority_score:.0f}/100")
    print(f"  adaptive_efficiency:     {report.adaptive_efficiency_score:.0f}/100")
    print(f"  strategies_stagnant:     {report.strategies_stagnant}/{report.strategies_analyzed}")
    print(f"  strategies_redundant:    {report.strategies_redundant}/{report.strategies_analyzed}")
    print(f"  strategies_converged:    {report.strategies_converged}/{report.strategies_analyzed}")
    if report.top_priority_strategy:
        print(f"  top_priority:            {report.top_priority_strategy}")
    if report.efficiency_bottleneck:
        print(f"  bottleneck:              {report.efficiency_bottleneck}")

    if report.profiles:
        print(f"\n{'Estrategia':<25} {'OptEff':>6} {'CompPrio':>8} {'AdaptEff':>8} {'Exp':>5} {'Stag':>5} {'Redund':>6}")
        print("-" * 70)
        for p in sorted(report.profiles, key=lambda x: x.computational_priority, reverse=True):
            flags = ("S" if p.is_stagnant else "-") + ("R" if p.is_redundant else "-")
            print(
                f"{p.strategy_id:<25} {p.optimization_efficiency:>6.0f} "
                f"{p.computational_priority:>8.0f} {p.adaptive_efficiency:>8.0f} "
                f"{p.experiments_count:>5} {flags:>5} {'Y' if p.is_redundant else 'N':>6}"
            )

    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

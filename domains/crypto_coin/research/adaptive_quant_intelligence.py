"""
adaptive_quant_intelligence.py — Phase M FASES 13-16 / Phase N FASES 8-9

Módulo consolidado de inteligência quant adaptativa.

Inclui:
  FASE 13 (Phase M): Adaptive Allocation Engine     — paper only, reduz peso de estratégias degradadas
  FASE 14 (Phase M): Continuous Research Loop       — automate replay→sweep→ranking→degradation→allocation
  FASE 15 (Phase M): Quant Recommendation Intelligence — sistema recomenda, humano decide
  FASE 16 (Phase M): Adaptive Portfolio Intelligence   — portfolio_health_score, diversification_quality
  FASE 8  (Phase N): Adaptive Portfolio Evolution      — resilience, drift-aware rebalance
  FASE 9  (Phase N): Extended Quant Recommendation Engine — lifecycle, exposure, drift signals

IMPORTANTE:
  - NÃO executa operações reais. Paper trading apenas.
  - NÃO usa RL, LLM ou predição mágica.
  - NÃO acessa exchanges públicas.
  - Toda recomendação requer confirmação humana antes de execução.

CLI:
  python -m domains.crypto_coin.research.adaptive_quant_intelligence --allocation --strategies trend_following breakout
  python -m domains.crypto_coin.research.adaptive_quant_intelligence --recommendations
  python -m domains.crypto_coin.research.adaptive_quant_intelligence --portfolio-health --strategies trend_following breakout
  python -m domains.crypto_coin.research.adaptive_quant_intelligence --research-loop --strategies trend_following
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.experiment_tracker          import ExperimentTracker
from domains.crypto_coin.research.strategy_degradation_intelligence import StrategyDegradationIntelligence, DegradationFleetAnalyzer
from domains.crypto_coin.research.fragility_intelligence      import FragilityIntelligenceAnalyzer
from domains.crypto_coin.research.regime_aware_intelligence   import RegimeAwareIntelligence

EXPERIMENTS_DIR = Path("data/experiments")


# ══════════════════════════════════════════════════════════════════════════════
# FASE 13 — Adaptive Allocation Engine (paper only)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AllocationWeight:
    strategy_id:         str
    base_weight:         float   # peso igual (1/n)
    adaptive_weight:     float   # peso ajustado por degradação
    health_score:        float
    degradation_score:   float
    adjustment_reason:   str


@dataclass
class AdaptiveAllocationPlan:
    """
    Plano de alocação paper-only gerado pelo motor adaptativo.
    NUNCA executa automaticamente. Requer aprovação humana.
    """
    strategies:          list[AllocationWeight]
    total_strategies:    int
    regime_context:      str | None
    adjustment_applied:  bool
    justification:       str
    warning:             str
    computed_at:         str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strategies"] = [asdict(w) for w in self.strategies]
        return d


class AdaptiveAllocationEngine:
    """
    FASE 13: Calcula pesos de alocação adaptativos (paper only).

    Princípio:
      - Começa com pesos iguais (1/n por estratégia)
      - Reduz peso de estratégias degradadas (health_score < 50)
      - Aumenta peso de estratégias saudáveis
      - Normaliza para somar 100%
      - NUNCA executa trades reais

    Constraints:
      - Peso máximo por estratégia: 60%
      - Peso mínimo por estratégia: 5%
    """

    MAX_WEIGHT = 0.60
    MIN_WEIGHT = 0.05

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def compute(
        self,
        strategy_ids: list[str],
        regime_context: str | None = None,
    ) -> AdaptiveAllocationPlan:
        if not strategy_ids:
            return self._empty_plan("Nenhuma estratégia fornecida")

        # Analisa saúde de cada estratégia
        health_data: list[dict] = []
        for sid in strategy_ids:
            try:
                analyzer = StrategyDegradationIntelligence(sid, self.experiments_dir)
                report   = analyzer.analyze()
                health_data.append({
                    "strategy_id":       sid,
                    "health_score":      report.strategy_health_score,
                    "degradation_score": report.degradation_score,
                })
            except Exception:
                health_data.append({
                    "strategy_id":       sid,
                    "health_score":      50.0,
                    "degradation_score": 0.0,
                })

        # Calcula peso bruto proporcional a health_score
        total_health = sum(d["health_score"] for d in health_data)
        if total_health == 0:
            total_health = len(health_data) * 50.0  # fallback neutro

        weights: list[AllocationWeight] = []
        adjustment_applied = False

        for d in health_data:
            base_w     = 1.0 / len(health_data)
            adaptive_w = (d["health_score"] / total_health)

            # Clamp
            adaptive_w = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, adaptive_w))

            reason = "saudável — peso mantido"
            if d["degradation_score"] >= 70:
                reason = f"degradação crítica ({d['degradation_score']:.0f}) — peso reduzido"
                adjustment_applied = True
            elif d["health_score"] < 50:
                reason = f"saúde baixa ({d['health_score']:.0f}) — peso levemente reduzido"
                adjustment_applied = True

            weights.append(AllocationWeight(
                strategy_id       = d["strategy_id"],
                base_weight       = round(base_w, 4),
                adaptive_weight   = round(adaptive_w, 4),
                health_score      = round(d["health_score"], 1),
                degradation_score = round(d["degradation_score"], 1),
                adjustment_reason = reason,
            ))

        # Renormaliza pesos adaptativos para somar 1.0
        total_w = sum(w.adaptive_weight for w in weights)
        if total_w > 0:
            for w in weights:
                w.adaptive_weight = round(w.adaptive_weight / total_w, 4)

        justification = (
            f"Pesos ajustados por degradação em {sum(1 for w in weights if 'reduzido' in w.adjustment_reason)} estratégia(s)"
            if adjustment_applied else
            "Todas as estratégias saudáveis — pesos adaptativos próximos ao equal-weight"
        )

        return AdaptiveAllocationPlan(
            strategies       = weights,
            total_strategies = len(weights),
            regime_context   = regime_context,
            adjustment_applied = adjustment_applied,
            justification    = justification,
            warning          = "⚠️ PAPER ONLY — Este plano requer aprovação humana antes de qualquer execução",
            computed_at      = datetime.now(timezone.utc).isoformat(),
        )

    def _empty_plan(self, reason: str) -> AdaptiveAllocationPlan:
        return AdaptiveAllocationPlan(
            strategies=[], total_strategies=0, regime_context=None,
            adjustment_applied=False, justification=reason,
            warning="⚠️ PAPER ONLY",
            computed_at=datetime.now(timezone.utc).isoformat(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# FASE 15 — Quant Recommendation Intelligence
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class QuantRecommendation:
    id:              str
    type:            str   # allocation | sweep | retire | investigate | monitor
    priority:        str   # critical | high | medium | low
    strategy_id:     str | None
    title:           str
    description:     str
    action:          str
    confidence:      str   # high | medium | low
    estimated_impact: str


@dataclass
class QuantRecommendationReport:
    recommendations:     list[QuantRecommendation]
    fleet_health_score:  float   # média ponderada de health_scores
    strategies_at_risk:  int
    computed_at:         str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["recommendations"] = [asdict(r) for r in self.recommendations]
        return d


class QuantRecommendationIntelligence:
    """
    FASE 15: Gera recomendações operacionais para o quant researcher.
    Sistema recomenda — humano decide. Nunca auto-executa.
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def generate(self, strategy_ids: list[str]) -> QuantRecommendationReport:
        recs: list[QuantRecommendation] = []
        fleet_health: list[float] = []
        at_risk = 0

        for sid in strategy_ids:
            try:
                # Degradation
                deg_analyzer = StrategyDegradationIntelligence(sid, self.experiments_dir)
                deg_report   = deg_analyzer.analyze()
                fleet_health.append(deg_report.strategy_health_score)

                if deg_report.composite_risk_score >= 70:
                    at_risk += 1
                    recs.append(QuantRecommendation(
                        id=f"retire_{sid}", type="retire", priority="critical",
                        strategy_id=sid,
                        title=f"Retirar '{sid}' — risco crítico",
                        description=f"Composite risk={deg_report.composite_risk_score:.0f}, health={deg_report.strategy_health_score:.0f}",
                        action=f"Suspender '{sid}' e executar novo sweep com parâmetros conservadores",
                        confidence="high", estimated_impact="Alto — evita exposição a estratégia degradada",
                    ))
                elif deg_report.composite_risk_score >= 40:
                    recs.append(QuantRecommendation(
                        id=f"investigate_{sid}", type="investigate", priority="high",
                        strategy_id=sid,
                        title=f"Investigar '{sid}' — risco moderado",
                        description=f"Risk={deg_report.composite_risk_score:.0f}, degradation={deg_report.degradation_score:.0f}",
                        action=f"Executar sweep ampliado e comparar com baseline anterior",
                        confidence="medium", estimated_impact="Médio — identificar causa da degradação",
                    ))

                # Fragility
                frag_analyzer = FragilityIntelligenceAnalyzer(sid, self.experiments_dir)
                frag_report   = frag_analyzer.analyze()

                if frag_report.overfitting_score >= 70:
                    recs.append(QuantRecommendation(
                        id=f"overfit_{sid}", type="investigate", priority="high",
                        strategy_id=sid,
                        title=f"'{sid}' com alta suspeita de overfitting",
                        description=f"Overfitting score={frag_report.overfitting_score:.0f}",
                        action="Validar em período out-of-sample não utilizado no sweep",
                        confidence="medium", estimated_impact="Alto — overfitting = performance irreal",
                    ))

                if frag_report.fragility_score >= 70:
                    recs.append(QuantRecommendation(
                        id=f"fragile_{sid}", type="sweep", priority="medium",
                        strategy_id=sid,
                        title=f"'{sid}' com parâmetros frágeis",
                        description=f"Fragility={frag_report.fragility_score:.0f}, std_sharpe={frag_report.sharpe_std}",
                        action="Executar sweep com grid mais amplo para encontrar região estável",
                        confidence="medium", estimated_impact="Médio — reduz dependência de parâmetros específicos",
                    ))

            except Exception as e:
                recs.append(QuantRecommendation(
                    id=f"error_{sid}", type="monitor", priority="low",
                    strategy_id=sid,
                    title=f"Erro ao analisar '{sid}'",
                    description=str(e), action="Verificar dados de experimentos",
                    confidence="low", estimated_impact="Baixo",
                ))

        # Recomendação global se frota toda está em risco
        fleet_avg = statistics.mean(fleet_health) if fleet_health else 50.0
        if fleet_avg < 40:
            recs.insert(0, QuantRecommendation(
                id="fleet_critical", type="investigate", priority="critical",
                strategy_id=None,
                title="Frota em estado crítico",
                description=f"Health médio da frota: {fleet_avg:.0f}/100",
                action="Pausar alocação adaptativa e revisar metodologia de sweep",
                confidence="high", estimated_impact="Crítico — toda a frota comprometida",
            ))

        # Ordena por prioridade
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        recs.sort(key=lambda r: order.get(r.priority, 99))

        return QuantRecommendationReport(
            recommendations    = recs,
            fleet_health_score = round(fleet_avg, 1),
            strategies_at_risk = at_risk,
            computed_at        = datetime.now(timezone.utc).isoformat(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# FASE 16 — Adaptive Portfolio Intelligence
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AdaptivePortfolioReport:
    """Saúde e qualidade adaptativa do portfólio de estratégias."""
    strategy_ids:                list[str]
    portfolio_health_score:      float   # 0–100
    diversification_quality_score: float # 0–100
    adaptive_portfolio_score:    float   # 0–100 (composto)

    # Componentes
    avg_strategy_health:   float
    strategies_healthy:    int   # health > 60
    strategies_at_risk:    int   # health < 40
    regime_coverage:       int   # quantos regimes únicos cobertos

    # Correlação (proxy: correlação de regime_performance)
    avg_regime_correlation: float | None   # 0–1, menor = mais diversificado

    recommendation:   str
    computed_at:      str

    def to_dict(self) -> dict:
        return asdict(self)


class AdaptivePortfolioIntelligence:
    """
    FASE 16: Avalia a saúde e diversificação do portfólio de estratégias.
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def analyze(self, strategy_ids: list[str]) -> AdaptivePortfolioReport:
        if not strategy_ids:
            return self._empty_report(strategy_ids)

        health_scores: list[float] = []
        regime_best: list[str | None] = []
        regime_sharpe_by_strategy: list[dict[str, float]] = []

        for sid in strategy_ids:
            try:
                deg = StrategyDegradationIntelligence(sid, self.experiments_dir).analyze()
                health_scores.append(deg.strategy_health_score)

                reg = RegimeAwareIntelligence(sid, self.experiments_dir).analyze()
                regime_best.append(reg.best_regime)
                regime_sharpe = {c.regime: c.avg_sharpe for c in reg.compatibility_matrix}
                regime_sharpe_by_strategy.append(regime_sharpe)
            except Exception:
                health_scores.append(50.0)
                regime_best.append(None)

        avg_health       = statistics.mean(health_scores) if health_scores else 50.0
        healthy_count    = sum(1 for h in health_scores if h >= 60)
        at_risk_count    = sum(1 for h in health_scores if h < 40)

        # Diversificação de regime: estratégias diferentes dominam regimes diferentes?
        unique_best_regimes = len(set(r for r in regime_best if r is not None))
        regime_coverage     = unique_best_regimes

        # Qualidade de diversificação: 100% se cada estratégia tem regime diferente
        diversification_quality = min(100.0, (unique_best_regimes / max(len(strategy_ids), 1)) * 100.0)

        # Correlação proxy: se duas estratégias são melhores no mesmo regime, elas são correlacionadas
        avg_regime_corr = None
        if len(regime_sharpe_by_strategy) >= 2:
            corr_scores: list[float] = []
            for i in range(len(regime_sharpe_by_strategy)):
                for j in range(i + 1, len(regime_sharpe_by_strategy)):
                    a = regime_sharpe_by_strategy[i]
                    b = regime_sharpe_by_strategy[j]
                    common = set(a.keys()) & set(b.keys())
                    if len(common) >= 3:
                        av = [a[r] for r in common]
                        bv = [b[r] for r in common]
                        corr = self._pearson(av, bv)
                        if corr is not None:
                            corr_scores.append(corr)
            if corr_scores:
                avg_regime_corr = round(statistics.mean(corr_scores), 3)

        # Portfolio health: média ponderada
        portfolio_health = round(avg_health * 0.6 + diversification_quality * 0.4, 1)

        # Adaptive portfolio score
        corr_penalty = (avg_regime_corr or 0.5) * 20   # alta correlação = penalidade
        adaptive_score = round(portfolio_health - corr_penalty + healthy_count * 3, 1)
        adaptive_score = max(0.0, min(100.0, adaptive_score))

        # Recomendação
        if at_risk_count >= len(strategy_ids) // 2:
            rec = f"Portfólio em risco: {at_risk_count}/{len(strategy_ids)} estratégias com health baixo"
        elif diversification_quality < 40:
            rec = f"Fraca diversificação de regime — adicionar estratégia adequada a: {', '.join(set(r for r in regime_best if r))}"
        else:
            rec = f"Portfólio saudável: {healthy_count}/{len(strategy_ids)} estratégias em boa forma"

        return AdaptivePortfolioReport(
            strategy_ids                = strategy_ids,
            portfolio_health_score      = portfolio_health,
            diversification_quality_score = round(diversification_quality, 1),
            adaptive_portfolio_score    = adaptive_score,
            avg_strategy_health         = round(avg_health, 1),
            strategies_healthy          = healthy_count,
            strategies_at_risk          = at_risk_count,
            regime_coverage             = regime_coverage,
            avg_regime_correlation      = avg_regime_corr,
            recommendation              = rec,
            computed_at                 = datetime.now(timezone.utc).isoformat(),
        )

    def _pearson(self, a: list[float], b: list[float]) -> float | None:
        if len(a) != len(b) or len(a) < 2:
            return None
        n   = len(a)
        avg_a = sum(a) / n
        avg_b = sum(b) / n
        num  = sum((a[i] - avg_a) * (b[i] - avg_b) for i in range(n))
        den  = (sum((x - avg_a) ** 2 for x in a) * sum((x - avg_b) ** 2 for x in b)) ** 0.5
        return round(num / den, 3) if den > 0 else 0.0

    def _empty_report(self, ids: list[str]) -> AdaptivePortfolioReport:
        return AdaptivePortfolioReport(
            strategy_ids=ids, portfolio_health_score=0.0,
            diversification_quality_score=0.0, adaptive_portfolio_score=0.0,
            avg_strategy_health=0.0, strategies_healthy=0, strategies_at_risk=0,
            regime_coverage=0, avg_regime_correlation=None,
            recommendation="Sem estratégias para analisar",
            computed_at=datetime.now(timezone.utc).isoformat(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# FASE 14 — Continuous Research Loop (runner)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ResearchLoopResult:
    """Resultado de uma iteração do loop de pesquisa contínua."""
    strategies_analyzed:   int
    degraded_strategies:   list[str]
    fragile_strategies:    list[str]
    recommendations:       int
    fleet_health_score:    float
    portfolio_health_score: float
    action_items:          list[str]
    loop_duration_ms:      float
    computed_at:           str

    def to_dict(self) -> dict:
        return asdict(self)


def run_research_loop(
    strategy_ids: list[str],
    experiments_dir: Path = EXPERIMENTS_DIR,
) -> ResearchLoopResult:
    """
    FASE 14: Executa uma iteração do loop de pesquisa contínua.

    Sequência:
      1. Analisa degradação de cada estratégia
      2. Verifica fragilidade
      3. Avalia portfólio
      4. Gera recomendações
      5. Retorna sumário com action items

    Não re-executa replays — usa dados existentes do ExperimentTracker.
    Para re-executar replays: use ResearchOrchestrator.
    """
    import time
    start_ms = time.time() * 1000

    # 1. Degradation sweep
    degraded: list[str] = []
    fragile: list[str] = []

    for sid in strategy_ids:
        try:
            deg = StrategyDegradationIntelligence(sid, experiments_dir).analyze()
            if deg.composite_risk_score >= 50:
                degraded.append(sid)

            fra = FragilityIntelligenceAnalyzer(sid, experiments_dir).analyze()
            if fra.fragility_score >= 60:
                fragile.append(sid)
        except Exception:
            pass

    # 2. Recommendations
    reco_engine = QuantRecommendationIntelligence(experiments_dir)
    reco_report = reco_engine.generate(strategy_ids)

    # 3. Portfolio
    portfolio = AdaptivePortfolioIntelligence(experiments_dir).analyze(strategy_ids)

    # 4. Action items (human-readable)
    action_items: list[str] = []
    for rec in reco_report.recommendations[:5]:  # top 5
        action_items.append(f"[{rec.priority.upper()}] {rec.title}: {rec.action}")

    elapsed_ms = time.time() * 1000 - start_ms

    return ResearchLoopResult(
        strategies_analyzed    = len(strategy_ids),
        degraded_strategies    = degraded,
        fragile_strategies     = fragile,
        recommendations        = len(reco_report.recommendations),
        fleet_health_score     = reco_report.fleet_health_score,
        portfolio_health_score = portfolio.portfolio_health_score,
        action_items           = action_items,
        loop_duration_ms       = round(elapsed_ms, 1),
        computed_at            = datetime.now(timezone.utc).isoformat(),
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Adaptive Quant Intelligence — Phase M FASES 13-16")
    parser.add_argument("--strategies", nargs="+", default=[], help="strategy_ids")
    parser.add_argument("--allocation",      action="store_true", help="FASE 13: Adaptive allocation plan")
    parser.add_argument("--recommendations", action="store_true", help="FASE 15: Quant recommendations")
    parser.add_argument("--portfolio-health", action="store_true", help="FASE 16: Portfolio health")
    parser.add_argument("--research-loop",   action="store_true", help="FASE 14: Research loop iteration")
    parser.add_argument("--regime-context",  help="Regime atual para contexto de alocação")
    parser.add_argument("--json",            action="store_true", help="Output JSON")
    args = parser.parse_args()

    # Descobre estratégias se não fornecidas
    strategy_ids = args.strategies or [
        f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"
    ]

    if not strategy_ids:
        print("Nenhuma estratégia encontrada. Execute sweep_runner primeiro.")
        return

    if args.allocation:
        engine = AdaptiveAllocationEngine()
        plan   = engine.compute(strategy_ids, regime_context=args.regime_context)
        if args.json:
            print(json.dumps(plan.to_dict(), indent=2))
        else:
            print(f"\n⚠️  {plan.warning}")
            print(f"\nAdaptive Allocation Plan — {plan.total_strategies} estratégias")
            print(f"Justificativa: {plan.justification}")
            print(f"\n{'Estratégia':<25} {'Base':>8} {'Adaptive':>10} {'Health':>8} {'Reason'}")
            print("-" * 80)
            for w in plan.strategies:
                print(
                    f"{w.strategy_id:<25} {w.base_weight:>8.1%} "
                    f"{w.adaptive_weight:>10.1%} {w.health_score:>8.0f} "
                    f"{w.adjustment_reason}"
                )

    elif args.recommendations:
        engine = QuantRecommendationIntelligence()
        report = engine.generate(strategy_ids)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\nQuant Recommendations — {len(report.recommendations)} recomendação(ões)")
            print(f"Fleet Health: {report.fleet_health_score:.0f}/100 | At Risk: {report.strategies_at_risk}")
            for rec in report.recommendations:
                print(f"\n  [{rec.priority.upper()}] {rec.title}")
                print(f"  → {rec.action}")
                print(f"  Confiança: {rec.confidence} | Impacto: {rec.estimated_impact}")

    elif args.portfolio_health:
        engine = AdaptivePortfolioIntelligence()
        report = engine.analyze(strategy_ids)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\nAdaptive Portfolio Intelligence")
            print(f"  portfolio_health:    {report.portfolio_health_score:.0f}/100")
            print(f"  diversification:     {report.diversification_quality_score:.0f}/100")
            print(f"  adaptive_score:      {report.adaptive_portfolio_score:.0f}/100")
            print(f"  avg_health:          {report.avg_strategy_health:.0f}")
            print(f"  healthy/at-risk:     {report.strategies_healthy}/{report.strategies_at_risk}")
            print(f"  regime_coverage:     {report.regime_coverage} regimes únicos")
            print(f"  → {report.recommendation}")

    elif args.research_loop:
        result = run_research_loop(strategy_ids)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"\nResearch Loop — {result.strategies_analyzed} estratégias")
            print(f"  fleet_health:   {result.fleet_health_score:.0f}/100")
            print(f"  portfolio:      {result.portfolio_health_score:.0f}/100")
            print(f"  degraded:       {result.degraded_strategies}")
            print(f"  fragile:        {result.fragile_strategies}")
            print(f"  Recomendações ({result.recommendations}):")
            for item in result.action_items:
                print(f"    • {item}")
            print(f"  Loop em {result.loop_duration_ms:.0f}ms")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════════════════
# FASE 8 (Phase N) — Adaptive Portfolio Evolution
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PortfolioEvolutionReport:
    """
    Relatório de evolução adaptativa do portfólio com awareness de drift e rebalance.
    Estende AdaptivePortfolioReport com scores de resiliência e drift.
    """
    strategy_ids:                 list[str]

    # Scores de Phase N
    portfolio_resilience_score:   float   # 0–100 (resistência a cenários adversos)
    adaptive_diversification_score: float # 0–100 (diversificação dinâmica pós-drift)
    portfolio_drift_score:        float   # 0–100 (drift acumulado no portfólio)

    # Rebalance recommendations
    rebalance_triggers:           list[str]   # motivos para rebalancear
    rebalance_urgency:            str         # immediate | scheduled | none
    drift_adjusted_weights:       dict[str, float]  # pesos pós-drift

    # Componentes de base
    portfolio_health_score:       float
    avg_strategy_health:          float
    strategies_healthy:           int
    strategies_at_risk:           int

    recommendation:               str
    warning:                      str
    computed_at:                  str

    def to_dict(self) -> dict:
        return asdict(self)


class AdaptivePortfolioEvolution:
    """
    FASE 8 (Phase N): Evolução adaptativa do portfólio.

    Adiciona ao AdaptivePortfolioIntelligence (Phase M):
      - degradation-aware rebalance: reduz peso de degradadas
      - drift-aware rebalance: detecta se drift global justifica rebalance
      - stress-aware rebalance: stress scenarios alteram a distribuição
      - portfolio_resilience_score: baseado em cobertura de regimes + saúde
      - portfolio_drift_score: acumulado de drift da frota
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir
        self._base_portfolio = AdaptivePortfolioIntelligence(experiments_dir)

    def analyze(
        self,
        strategy_ids:   list[str],
        market_drift_score: float = 0.0,
    ) -> PortfolioEvolutionReport:
        """Análise de evolução adaptativa do portfólio."""
        base = self._base_portfolio.analyze(strategy_ids)

        # ── Portfolio drift score ─────────────────────────────────────────────
        # Combina market_drift com degradação interna do portfólio
        internal_drift = max(0.0, 100.0 - base.portfolio_health_score)
        portfolio_drift = round(market_drift_score * 0.5 + internal_drift * 0.5, 1)

        # ── Portfolio resilience score ────────────────────────────────────────
        # Regime coverage (base.regime_coverage) + health
        regime_coverage_score = min(100.0, base.regime_coverage * (100.0 / 6))  # 6 regimes conhecidos
        resilience = round(
            base.portfolio_health_score * 0.5
            + regime_coverage_score * 0.3
            + max(0.0, 100.0 - (base.avg_regime_correlation or 0.5) * 100) * 0.2,
            1,
        )

        # ── Adaptive diversification score ────────────────────────────────────
        # Penaliza se drift elevado reduziu cobertura de regime
        drift_penalty = min(20.0, market_drift_score * 0.2)
        adaptive_div  = round(
            max(0.0, base.diversification_quality_score - drift_penalty), 1
        )

        # ── Rebalance triggers ────────────────────────────────────────────────
        triggers: list[str] = []
        if base.strategies_at_risk > 0:
            triggers.append(f"{base.strategies_at_risk} estratégia(s) com health baixo")
        if market_drift_score >= 40:
            triggers.append(f"Drift de mercado elevado ({market_drift_score:.0f}/100)")
        if portfolio_drift >= 50:
            triggers.append(f"Portfolio drift acumulado ({portfolio_drift:.0f}/100)")
        if (base.avg_regime_correlation or 0) > 0.7:
            triggers.append(f"Correlação de regime alta ({base.avg_regime_correlation:.2f})")

        # ── Rebalance urgency ─────────────────────────────────────────────────
        if len([t for t in triggers if any(kw in t for kw in ["health baixo", "crítico"])]) > 0 \
                or market_drift_score >= 70:
            rebalance_urgency = "immediate"
        elif len(triggers) >= 2 or market_drift_score >= 40:
            rebalance_urgency = "scheduled"
        else:
            rebalance_urgency = "none"

        # ── Drift-adjusted weights (paper only) ───────────────────────────────
        # Penaliza estratégias degradadas, beneficia estratégias saudáveis
        drift_weights: dict[str, float] = {}
        if strategy_ids:
            health_scores: list[float] = []
            for sid in strategy_ids:
                try:
                    deg = StrategyDegradationIntelligence(sid, self.experiments_dir).analyze()
                    health_scores.append(deg.strategy_health_score)
                except Exception:
                    health_scores.append(50.0)

            total_h = sum(health_scores) or (len(health_scores) * 50.0)
            for sid, h in zip(strategy_ids, health_scores):
                raw_w = h / total_h
                drift_factor = max(0.5, 1.0 - market_drift_score / 200.0)  # reduz até 50% em drift=100
                drift_weights[sid] = round(raw_w * drift_factor, 4)

            # Renormaliza
            total_w = sum(drift_weights.values())
            if total_w > 0:
                drift_weights = {k: round(v / total_w, 4) for k, v in drift_weights.items()}

        # ── Recommendation ────────────────────────────────────────────────────
        if rebalance_urgency == "immediate":
            recommendation = (
                "Rebalance imediato recomendado. "
                + "; ".join(triggers[:2])
                + " ⚠️ PAPER ONLY — Requer aprovação humana."
            )
        elif rebalance_urgency == "scheduled":
            recommendation = f"Agendar rebalance no próximo ciclo. Motivos: {', '.join(triggers)}."
        else:
            recommendation = "Portfólio em equilíbrio — continuar monitoramento regular."

        return PortfolioEvolutionReport(
            strategy_ids                  = strategy_ids,
            portfolio_resilience_score    = resilience,
            adaptive_diversification_score= adaptive_div,
            portfolio_drift_score         = portfolio_drift,
            rebalance_triggers            = triggers,
            rebalance_urgency             = rebalance_urgency,
            drift_adjusted_weights        = drift_weights,
            portfolio_health_score        = base.portfolio_health_score,
            avg_strategy_health           = base.avg_strategy_health,
            strategies_healthy            = base.strategies_healthy,
            strategies_at_risk            = base.strategies_at_risk,
            recommendation                = recommendation,
            warning                       = "⚠️ PAPER ONLY — Requer aprovação humana antes de rebalancear",
            computed_at                   = datetime.now(timezone.utc).isoformat(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# FASE 9 (Phase N) — Extended Quant Recommendation Engine
# ══════════════════════════════════════════════════════════════════════════════

class QuantRecommendationEngineV2(QuantRecommendationIntelligence):
    """
    FASE 9 (Phase N): Recommendation Engine Consolidado.

    Estende QuantRecommendationIntelligence (Phase M) com:
      - sinais de lifecycle (lifecycle_state → lifecycle_transition)
      - sinais de exposure (exposure_cap reduzida)
      - sinais de drift (market drift → atenção)
      - sinais de meta-strategy (pares conflitantes)

    Herança: reutiliza generate() e toda a lógica de Phase M.
    Adiciona: extend_with_context() para sinais de Phase N.
    """

    def generate_v2(
        self,
        strategy_ids:        list[str],
        market_drift_score:  float = 0.0,
        lifecycle_states:    dict[str, str] | None = None,  # {strategy_id: state}
        conflicting_pairs:   list[str] | None = None,
    ) -> "QuantRecommendationReport":
        """
        Gera recomendações consolidadas v2 (Phase M base + Phase N context).
        """
        # Base recommendations da Phase M
        base_report = self.generate(strategy_ids)
        recs        = list(base_report.recommendations)

        # ── Drift global ───────────────────────────────────────────────────────
        if market_drift_score >= 70:
            recs.insert(0, QuantRecommendation(
                id="market_drift_critical", type="investigate", priority="critical",
                strategy_id=None,
                title="Drift de mercado crítico detectado",
                description=f"market_drift_score={market_drift_score:.0f}/100",
                action=(
                    "Pausar novos rebalances. Executar drift analysis completo. "
                    "Reduzir exposure de estratégias degradadas."
                ),
                confidence="high",
                estimated_impact="Crítico — preservação de capital",
            ))
        elif market_drift_score >= 40:
            recs.append(QuantRecommendation(
                id="market_drift_high", type="monitor", priority="high",
                strategy_id=None,
                title="Drift de mercado elevado",
                description=f"market_drift_score={market_drift_score:.0f}/100",
                action="Monitorar de perto. Aplicar caps de exposure conservadores.",
                confidence="medium",
                estimated_impact="Médio — proteção de downside",
            ))

        # ── Lifecycle transitions ──────────────────────────────────────────────
        if lifecycle_states:
            for sid, state in lifecycle_states.items():
                if state == "retired":
                    recs.append(QuantRecommendation(
                        id=f"lifecycle_retired_{sid}", type="retire", priority="critical",
                        strategy_id=sid,
                        title=f"'{sid}' atingiu estado RETIRED",
                        description=f"Lifecycle state = retired. Exposição deve ser zero.",
                        action=f"Remover '{sid}' do portfólio ativo imediatamente.",
                        confidence="high",
                        estimated_impact="Alto — estratégia sem viabilidade atual",
                    ))
                elif state == "degraded":
                    recs.append(QuantRecommendation(
                        id=f"lifecycle_degraded_{sid}", type="investigate", priority="high",
                        strategy_id=sid,
                        title=f"'{sid}' em estado DEGRADED",
                        description=f"Lifecycle state = degraded. Exposure cap = 20%.",
                        action=f"Aplicar cap de 20% e investigar causa da degradação.",
                        confidence="high",
                        estimated_impact="Médio — exposição controlada",
                    ))

        # ── Conflicting pairs ──────────────────────────────────────────────────
        if conflicting_pairs:
            for pair_str in conflicting_pairs[:3]:  # top 3
                recs.append(QuantRecommendation(
                    id=f"conflict_{pair_str.replace(' ', '_')}",
                    type="investigate", priority="medium",
                    strategy_id=None,
                    title=f"Par redundante detectado: {pair_str}",
                    description=f"Correlação de regime > 0.8. Estratégias podem ser redundantes.",
                    action="Avaliar se uma das estratégias pode ser removida ou substituída.",
                    confidence="medium",
                    estimated_impact="Baixo — eficiência de portfólio",
                ))

        # Reordena por prioridade
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        recs.sort(key=lambda r: order.get(r.priority, 99))

        # Emite contador de recomendações
        try:
            from api.metrics import autonomous_recommendations_total
            autonomous_recommendations_total.labels(type="quant").inc(len(recs))
        except Exception:
            pass

        return QuantRecommendationReport(
            recommendations    = recs,
            fleet_health_score = base_report.fleet_health_score,
            strategies_at_risk = base_report.strategies_at_risk,
            computed_at        = datetime.now(timezone.utc).isoformat(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# FASE 4 (Phase O) — Autonomous Portfolio Governor scores
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PortfolioGovernanceReport:
    """
    Scores de governança autônoma do portfólio (Phase O FASE 4).
    Estende PortfolioEvolutionReport com survival e stress scores.
    """
    strategy_ids:             list[str]
    portfolio_survival_score: float    # 0–100 (0 = portfólio em colapso)
    adaptive_resilience_score: float   # 0–100 (resiliência adaptativa post-stress)
    portfolio_stress_score:   float    # 0–100 (100 = máximo stress)

    # Triggers de governança autónoma
    auto_rebalance_triggered: bool
    auto_reduce_triggered:    bool
    governance_mode:          str    # normal | rebalance | reduce | protect

    # Base
    portfolio_health_score:   float
    portfolio_resilience_score: float
    portfolio_drift_score:    float
    rebalance_urgency:        str

    drift_adjusted_weights:   dict[str, float]
    justification:            str
    warning:                  str
    computed_at:              str

    def to_dict(self) -> dict:
        return asdict(self)


class AutonomousPortfolioGovernor:
    """
    FASE 4 (Phase O): Governanca autonoma do portfolio.

    Adiciona ao AdaptivePortfolioEvolution (Phase N):
      - portfolio_survival_score: 0 = portfólio em colapso iminente
      - adaptive_resilience_score: resiliência pós-aplicação de controles
      - portfolio_stress_score: nível de stress atual
      - auto-rebalance e auto-reduce triggers (paper only)
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir  = experiments_dir
        self._evolution       = AdaptivePortfolioEvolution(experiments_dir)

    def govern(
        self,
        strategy_ids:     list[str],
        market_drift:     float = 0.0,
        fleet_health_avg: float = 50.0,
    ) -> PortfolioGovernanceReport:
        base = self._evolution.analyze(strategy_ids, market_drift)

        # ── Portfolio stress score ─────────────────────────────────────────────
        stress = round(
            market_drift * 0.4
            + base.portfolio_drift_score * 0.3
            + max(0.0, 100.0 - base.portfolio_health_score) * 0.3,
            1,
        )

        # ── Portfolio survival score ───────────────────────────────────────────
        survival = round(max(0.0, 100.0 - stress * 0.6 - max(0.0, 50.0 - fleet_health_avg) * 0.8), 1)

        # ── Adaptive resilience score ──────────────────────────────────────────
        # After applying controls, how resilient is the portfolio?
        resilience_boost = max(0.0, 100.0 - market_drift) * 0.2
        adaptive_resilience = round(min(100.0, base.portfolio_resilience_score + resilience_boost), 1)

        # ── Governance triggers ────────────────────────────────────────────────
        auto_rebalance = base.rebalance_urgency in ("immediate", "scheduled")
        auto_reduce    = stress >= 65 or survival <= 35

        if auto_reduce:
            governance_mode = "protect"
        elif auto_rebalance and stress >= 40:
            governance_mode = "reduce"
        elif auto_rebalance:
            governance_mode = "rebalance"
        else:
            governance_mode = "normal"

        justification = (
            f"survival={survival:.0f}, stress={stress:.0f}, "
            f"drift={market_drift:.0f}, mode={governance_mode}"
        )

        # Prometheus
        try:
            from api.metrics import portfolio_survival_score as _ps
            _ps.set(survival)
        except Exception:
            pass

        return PortfolioGovernanceReport(
            strategy_ids              = strategy_ids,
            portfolio_survival_score  = survival,
            adaptive_resilience_score = adaptive_resilience,
            portfolio_stress_score    = stress,
            auto_rebalance_triggered  = auto_rebalance,
            auto_reduce_triggered     = auto_reduce,
            governance_mode           = governance_mode,
            portfolio_health_score    = base.portfolio_health_score,
            portfolio_resilience_score= base.portfolio_resilience_score,
            portfolio_drift_score     = base.portfolio_drift_score,
            rebalance_urgency         = base.rebalance_urgency,
            drift_adjusted_weights    = base.drift_adjusted_weights,
            justification             = justification,
            warning                   = "PAPER ONLY — governanca autonoma sem execucao real",
            computed_at               = datetime.now(timezone.utc).isoformat(),
        )

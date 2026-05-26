"""
autonomous_research_loop.py — Phase N FASE 10

Autonomous Research Loop — loop quantitativo contínuo e semi-autônomo.

Fluxo de cada iteração:
  market drift analysis
  → replay prioritization
  → lifecycle evaluation
  → exposure intelligence
  → parameter intelligence
  → portfolio evolution
  → ranking refresh
  → recommendation refresh (v2)

Características:
  - Cada ciclo produz um AutonomousResearchReport com lineage completo
  - Persiste histórico em data/autonomous_loop_history.jsonl
  - Emite métricas Prometheus em cada ciclo
  - Scheduling: cron-ready (pode ser chamado por scheduler externo)
  - Nunca executa trades ou mudanças de posição reais

IMPORTANTE:
  - NÃO executa live trading.
  - NÃO toma decisões sem aprovação humana.
  - Produz recomendações e relatórios — operador decide.

CLI:
  python -m domains.crypto_coin.research.autonomous_research_loop
  python -m domains.crypto_coin.research.autonomous_research_loop --strategies trend_following
  python -m domains.crypto_coin.research.autonomous_research_loop --json
  python -m domains.crypto_coin.research.autonomous_research_loop --history --days 7
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.market_drift_intelligence   import MarketDriftIntelligence
from domains.crypto_coin.research.strategy_lifecycle          import StrategyLifecycleEngine
from domains.crypto_coin.research.research_prioritizer        import ResearchPrioritizer
from domains.crypto_coin.research.parameter_intelligence      import ParameterIntelligenceFleet
from domains.crypto_coin.research.adaptive_exposure_intelligence import AdaptiveExposureIntelligence
from domains.crypto_coin.research.meta_strategy_intelligence  import MetaStrategyIntelligence
from domains.crypto_coin.research.adaptive_quant_intelligence import (
    AdaptivePortfolioEvolution,
    QuantRecommendationEngineV2,
)

EXPERIMENTS_DIR  = Path("data/experiments")
LOOP_HISTORY_FILE = Path("data/autonomous_loop_history.jsonl")

# Prometheus (optional)
try:
    from api.metrics import (
        research_loop_runs_total         as _prom_loop_runs,
        autonomous_recommendations_total as _prom_recs,
        portfolio_resilience_score       as _prom_resilience,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class LoopPhaseResult:
    """Resultado de uma fase do loop autônomo."""
    phase:       str
    success:     bool
    duration_ms: float
    summary:     dict
    error:       str | None = None


@dataclass
class AutonomousResearchReport:
    """
    Relatório completo de uma iteração do loop de research autônomo.

    Contém todos os sinais, recomendações e lineage do ciclo.
    """
    loop_id:                str
    started_at:             str
    completed_at:           str
    total_duration_ms:      float

    # Estratégias analisadas
    strategy_ids:           list[str]

    # Sinais principais
    market_drift_score:     float
    fleet_health_score:     float
    portfolio_resilience:   float
    portfolio_drift:        float
    rebalance_urgency:      str

    # Lifecycle
    states_by_strategy:     dict[str, str]   # {strategy_id: lifecycle_state}
    strategies_retired:     list[str]
    strategies_degraded:    list[str]

    # Research priorities
    critical_tasks:         int
    high_tasks:             int
    top_priority_task:      str | None

    # Recommendations
    total_recommendations:  int
    critical_recommendations: int

    # Meta
    conflicting_pairs:      list[str]
    best_combination:       list[str]

    # Phases detail
    phases:                 list[LoopPhaseResult]

    # Consolidated action items (human-readable)
    action_items:           list[str]

    success:                bool
    warning:                str
    loop_version:           str = "Phase N FASE 10"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phases"] = [asdict(p) for p in self.phases]
        return d


# ── Scheduler ─────────────────────────────────────────────────────────────────

class AutonomousResearchScheduler:
    """
    FASE 10: Executa ciclos de research autônomo semi-supervisionado.

    Cada ciclo:
      1. Analisa drift de mercado (MarketDriftIntelligence)
      2. Avalia lifecycle de estratégias (StrategyLifecycleEngine)
      3. Prioriza tarefas de research (ResearchPrioritizer)
      4. Analisa parâmetros (ParameterIntelligenceFleet)
      5. Calcula exposição adaptativa (AdaptiveExposureIntelligence)
      6. Analisa meta-estratégias (MetaStrategyIntelligence)
      7. Evolui portfólio (AdaptivePortfolioEvolution)
      8. Gera recomendações consolidadas v2 (QuantRecommendationEngineV2)
      9. Persiste resultado com lineage
    """

    def __init__(
        self,
        experiments_dir:   Path = EXPERIMENTS_DIR,
        loop_history_file: Path = LOOP_HISTORY_FILE,
        current_regime:    str | None = None,
    ):
        self.experiments_dir   = experiments_dir
        self.loop_history_file = loop_history_file
        self.current_regime    = current_regime

    def run(self, strategy_ids: list[str] | None = None) -> AutonomousResearchReport:
        """
        Executa uma iteração completa do loop autônomo.

        Se strategy_ids não fornecido, descobre automaticamente.
        """
        loop_id    = str(uuid.uuid4())[:8]
        started_at = datetime.now(timezone.utc).isoformat()
        start_ms   = time.time() * 1000

        # Auto-discover strategies
        if not strategy_ids:
            strategy_files = list(self.experiments_dir.glob("*.jsonl"))
            strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]

        phases: list[LoopPhaseResult] = []
        market_drift_score   = 0.0
        fleet_health_score   = 50.0
        portfolio_resilience = 50.0
        portfolio_drift      = 0.0
        rebalance_urgency    = "none"
        states_by_strategy:  dict[str, str] = {}
        strategies_retired:  list[str] = []
        strategies_degraded: list[str] = []
        critical_tasks       = 0
        high_tasks           = 0
        top_priority_task: str | None = None
        total_recommendations = 0
        critical_recommendations = 0
        conflicting_pairs: list[str] = []
        best_combination:  list[str] = list(strategy_ids)
        action_items: list[str] = []

        # ── FASE 1: Market Drift ──────────────────────────────────────────────
        t0 = time.time()
        try:
            drift_report     = MarketDriftIntelligence(self.experiments_dir).analyze()
            market_drift_score = drift_report.market_drift_score
            fleet_health_score = drift_report.fleet_health_avg
            phases.append(LoopPhaseResult(
                phase="market_drift", success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={
                    "market_drift_score": drift_report.market_drift_score,
                    "edge_decay_score":   drift_report.edge_decay_score,
                    "strategies_analyzed": drift_report.strategies_analyzed,
                },
            ))
            if drift_report.market_drift_score >= 70:
                action_items.append(
                    f"[CRITICAL] Drift crítico ({drift_report.market_drift_score:.0f}/100): {drift_report.recommendation}"
                )
        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="market_drift", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))

        # ── FASE 2: Lifecycle Evaluation ──────────────────────────────────────
        t0 = time.time()
        try:
            lc_engine = StrategyLifecycleEngine(self.experiments_dir)
            for sid in strategy_ids:
                try:
                    lc_status = lc_engine.evaluate(sid)
                    states_by_strategy[sid] = lc_status.lifecycle_state
                    if lc_status.lifecycle_state == "retired":
                        strategies_retired.append(sid)
                        action_items.append(f"[CRITICAL] Retirar '{sid}' do portfólio (lifecycle=retired)")
                    elif lc_status.lifecycle_state == "degraded":
                        strategies_degraded.append(sid)
                        action_items.append(f"[HIGH] Reduzir exposure de '{sid}' (lifecycle=degraded)")
                except Exception:
                    states_by_strategy[sid] = "experimental"

            phases.append(LoopPhaseResult(
                phase="lifecycle", success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={
                    "retired":  len(strategies_retired),
                    "degraded": len(strategies_degraded),
                    "states":   states_by_strategy,
                },
            ))
        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="lifecycle", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))

        # ── FASE 3: Research Prioritization ──────────────────────────────────
        t0 = time.time()
        try:
            prio_report  = ResearchPrioritizer(self.experiments_dir).generate()
            critical_tasks = prio_report.critical_tasks
            high_tasks     = prio_report.high_tasks
            if prio_report.tasks:
                top = prio_report.tasks[0]
                top_priority_task = f"[{top.priority.upper()}] {top.task_type}:{top.strategy_id} — {top.reason}"
                if top.priority in ("critical", "high"):
                    action_items.append(f"[{top.priority.upper()}] Research: {top.suggested_action}")
            phases.append(LoopPhaseResult(
                phase="research_prioritization", success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={
                    "critical_tasks":         critical_tasks,
                    "high_tasks":             high_tasks,
                    "fleet_research_urgency": prio_report.fleet_research_urgency,
                },
            ))
        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="research_prioritization", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))

        # ── FASE 4: Parameter Intelligence ───────────────────────────────────
        t0 = time.time()
        try:
            param_fleet   = ParameterIntelligenceFleet(self.experiments_dir)
            param_reports = param_fleet.analyze_all()
            fragile_count = sum(1 for r in param_reports if r.top_fragile_params)
            phases.append(LoopPhaseResult(
                phase="parameter_intelligence", success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={
                    "strategies_with_fragile_params": fragile_count,
                    "total_analyzed": len(param_reports),
                },
            ))
            if fragile_count > 0:
                action_items.append(
                    f"[MEDIUM] {fragile_count} estratégia(s) com parâmetros frágeis — executar sweep ampliado"
                )
        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="parameter_intelligence", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))

        # ── FASE 5: Adaptive Exposure ─────────────────────────────────────────
        t0 = time.time()
        try:
            exposure_engine = AdaptiveExposureIntelligence(
                experiments_dir=self.experiments_dir,
                current_regime=self.current_regime,
            )
            exposure_report = exposure_engine.analyze(strategy_ids)
            if exposure_report.portfolio_stress_level in ("high", "critical"):
                action_items.append(
                    f"[HIGH] Stress {exposure_report.portfolio_stress_level}: "
                    f"reduzir exposure ({exposure_report.recommendation})"
                )
            phases.append(LoopPhaseResult(
                phase="adaptive_exposure", success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={
                    "stress_level":      exposure_report.portfolio_stress_level,
                    "total_exposure":    exposure_report.total_recommended_exposure,
                    "market_drift":      exposure_report.market_drift_score,
                },
            ))
        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="adaptive_exposure", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))

        # ── FASE 6: Meta-Strategy Intelligence ───────────────────────────────
        t0 = time.time()
        try:
            if len(strategy_ids) >= 2:
                meta_engine  = MetaStrategyIntelligence(self.experiments_dir)
                meta_report  = meta_engine.analyze(strategy_ids)
                conflicting_pairs = meta_report.conflicting_pairs
                best_combination  = meta_report.best_combination
                if conflicting_pairs:
                    action_items.append(
                        f"[LOW] Pares redundantes: {', '.join(conflicting_pairs[:2])} — avaliar simplificação"
                    )
                phases.append(LoopPhaseResult(
                    phase="meta_strategy", success=True,
                    duration_ms=round((time.time() - t0) * 1000, 1),
                    summary={
                        "synergy_score":    meta_report.diversification_synergy_score,
                        "conflicting":      len(conflicting_pairs),
                        "best_combination": best_combination,
                    },
                ))
            else:
                phases.append(LoopPhaseResult(
                    phase="meta_strategy", success=True,
                    duration_ms=0.0,
                    summary={"skipped": "Mínimo 2 estratégias necessárias"},
                ))
        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="meta_strategy", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))

        # ── FASE 7: Portfolio Evolution ───────────────────────────────────────
        t0 = time.time()
        try:
            portfolio_engine = AdaptivePortfolioEvolution(self.experiments_dir)
            portfolio_report = portfolio_engine.analyze(strategy_ids, market_drift_score)
            portfolio_resilience = portfolio_report.portfolio_resilience_score
            portfolio_drift      = portfolio_report.portfolio_drift_score
            rebalance_urgency    = portfolio_report.rebalance_urgency

            if portfolio_report.rebalance_urgency == "immediate":
                action_items.append(
                    f"[CRITICAL] Rebalance imediato: {portfolio_report.recommendation}"
                )
            elif portfolio_report.rebalance_urgency == "scheduled":
                action_items.append(
                    f"[MEDIUM] Rebalance agendado: {portfolio_report.recommendation}"
                )

            # Emite métrica
            if _METRICS_AVAILABLE:
                try:
                    _prom_resilience.set(portfolio_resilience)
                except Exception:
                    pass

            phases.append(LoopPhaseResult(
                phase="portfolio_evolution", success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={
                    "portfolio_resilience":    portfolio_resilience,
                    "portfolio_drift":         portfolio_drift,
                    "rebalance_urgency":       rebalance_urgency,
                    "portfolio_health":        portfolio_report.portfolio_health_score,
                },
            ))
        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="portfolio_evolution", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))

        # ── FASE 8: Recommendations V2 ────────────────────────────────────────
        t0 = time.time()
        try:
            rec_engine = QuantRecommendationEngineV2(self.experiments_dir)
            rec_report = rec_engine.generate_v2(
                strategy_ids       = strategy_ids,
                market_drift_score = market_drift_score,
                lifecycle_states   = states_by_strategy,
                conflicting_pairs  = conflicting_pairs,
            )
            total_recommendations    = len(rec_report.recommendations)
            critical_recommendations = sum(
                1 for r in rec_report.recommendations if r.priority == "critical"
            )
            fleet_health_score = rec_report.fleet_health_score

            phases.append(LoopPhaseResult(
                phase="recommendations_v2", success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={
                    "total":    total_recommendations,
                    "critical": critical_recommendations,
                    "fleet_health": fleet_health_score,
                },
            ))

            # Emite contadores
            if _METRICS_AVAILABLE:
                try:
                    _prom_loop_runs.labels(status="success").inc()
                    if total_recommendations > 0:
                        _prom_recs.labels(type="quant").inc(total_recommendations)
                except Exception:
                    pass

        except Exception as e:
            phases.append(LoopPhaseResult(
                phase="recommendations_v2", success=False,
                duration_ms=round((time.time() - t0) * 1000, 1),
                summary={}, error=str(e),
            ))
            if _METRICS_AVAILABLE:
                try:
                    _prom_loop_runs.labels(status="error").inc()
                except Exception:
                    pass

        # ── Finaliza ──────────────────────────────────────────────────────────
        total_ms    = round(time.time() * 1000 - start_ms, 1)
        completed_at = datetime.now(timezone.utc).isoformat()
        success      = all(p.success for p in phases if p.phase != "meta_strategy")

        report = AutonomousResearchReport(
            loop_id                  = loop_id,
            started_at               = started_at,
            completed_at             = completed_at,
            total_duration_ms        = total_ms,
            strategy_ids             = strategy_ids,
            market_drift_score       = round(market_drift_score, 1),
            fleet_health_score       = round(fleet_health_score, 1),
            portfolio_resilience     = round(portfolio_resilience, 1),
            portfolio_drift          = round(portfolio_drift, 1),
            rebalance_urgency        = rebalance_urgency,
            states_by_strategy       = states_by_strategy,
            strategies_retired       = strategies_retired,
            strategies_degraded      = strategies_degraded,
            critical_tasks           = critical_tasks,
            high_tasks               = high_tasks,
            top_priority_task        = top_priority_task,
            total_recommendations    = total_recommendations,
            critical_recommendations = critical_recommendations,
            conflicting_pairs        = conflicting_pairs,
            best_combination         = best_combination,
            phases                   = phases,
            action_items             = action_items,
            success                  = success,
            warning                  = "⚠️ AUTONOMOUS LOOP — Sistema recomenda. Humano decide.",
        )

        self._persist(report)
        return report

    def _persist(self, report: AutonomousResearchReport) -> None:
        """Persiste o relatório do loop para rastreabilidade."""
        try:
            self.loop_history_file.parent.mkdir(parents=True, exist_ok=True)
            summary = {
                "loop_id":               report.loop_id,
                "completed_at":          report.completed_at,
                "total_duration_ms":     report.total_duration_ms,
                "market_drift_score":    report.market_drift_score,
                "fleet_health_score":    report.fleet_health_score,
                "portfolio_resilience":  report.portfolio_resilience,
                "rebalance_urgency":     report.rebalance_urgency,
                "total_recommendations": report.total_recommendations,
                "critical_tasks":        report.critical_tasks,
                "success":               report.success,
            }
            with open(self.loop_history_file, "a") as f:
                f.write(json.dumps(summary) + "\n")
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Research Loop — Phase N FASE 10"
    )
    parser.add_argument("--strategies", nargs="+", help="strategy_ids (auto-discover se omitido)")
    parser.add_argument("--regime",   help="Regime atual (ex: bull_market)")
    parser.add_argument("--json",     action="store_true", help="Output completo em JSON")
    parser.add_argument("--history",  action="store_true", help="Mostrar histórico de loops")
    parser.add_argument("--days",     type=int, default=30, help="Janela de histórico (--history)")
    args = parser.parse_args()

    if args.history:
        if not LOOP_HISTORY_FILE.exists():
            print("Nenhum histórico disponível. Execute o loop primeiro.")
            return
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        entries: list[dict] = []
        with open(LOOP_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        e = json.loads(line)
                        if e.get("completed_at", "") >= cutoff:
                            entries.append(e)
                    except Exception:
                        pass

        if args.json:
            print(json.dumps(entries, indent=2))
            return

        print(f"\nAutonomous Loop History — últimos {args.days} dias ({len(entries)} ciclos)")
        print(f"{'Loop ID':<10} {'Drift':>6} {'Health':>7} {'Resilience':>11} {'Recs':>5} {'Durms':>8}")
        print("-" * 55)
        for e in entries[-20:]:
            print(
                f"{e.get('loop_id', '?'):<10} {e.get('market_drift_score', 0):>6.0f} "
                f"{e.get('fleet_health_score', 0):>7.0f} {e.get('portfolio_resilience', 0):>11.0f} "
                f"{e.get('total_recommendations', 0):>5} {e.get('total_duration_ms', 0):>8.0f}"
            )
        return

    scheduler = AutonomousResearchScheduler(
        current_regime=args.regime,
    )
    print("Executando loop autônomo de research...")
    report = scheduler.run(strategy_ids=args.strategies)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\n{'='*60}")
    print(f"⚠️  {report.warning}")
    print(f"{'='*60}")
    print(f"\nAutonomous Research Loop — ID: {report.loop_id}")
    print(f"  Estratégias: {len(report.strategy_ids)} | Duração: {report.total_duration_ms:.0f}ms")
    print(f"\nSinais principais:")
    print(f"  market_drift_score:   {report.market_drift_score:.0f}/100")
    print(f"  fleet_health_score:   {report.fleet_health_score:.0f}/100")
    print(f"  portfolio_resilience: {report.portfolio_resilience:.0f}/100")
    print(f"  portfolio_drift:      {report.portfolio_drift:.0f}/100")
    print(f"  rebalance_urgency:    {report.rebalance_urgency}")

    print(f"\nLifecycle:")
    print(f"  retired:   {report.strategies_retired or 'nenhuma'}")
    print(f"  degraded:  {report.strategies_degraded or 'nenhuma'}")

    print(f"\nResearch:")
    print(f"  critical_tasks:  {report.critical_tasks}")
    print(f"  high_tasks:      {report.high_tasks}")
    if report.top_priority_task:
        print(f"  top_task:        {report.top_priority_task}")

    print(f"\nRecomendações: {report.total_recommendations} ({report.critical_recommendations} críticas)")

    if report.action_items:
        print(f"\nAction Items ({len(report.action_items)}):")
        for item in report.action_items:
            print(f"  • {item}")

    print(f"\nFases: {sum(1 for p in report.phases if p.success)}/{len(report.phases)} OK")
    failed = [p for p in report.phases if not p.success]
    if failed:
        print(f"  Falhas: {', '.join(p.phase for p in failed)}")

    print(f"\n{'='*60}")


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════════════════
# FASE 6 (Phase O) — Autonomous Research Evolution
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ResearchEvolutionPlan:
    """
    Plano de evolução de research gerado autonomamente.
    O sistema decide quais sweeps executar, quais validar, quais cenários simular.
    """
    strategy_priorities:      list[dict]    # [{strategy_id, priority, reason, suggested_cmd}]
    scenarios_to_simulate:    list[str]     # nomes de cenários prioritários
    parameters_to_sweep:      list[dict]    # [{strategy_id, parameter, range}]
    datasets_to_validate:     list[str]     # symbols/timeframes a validar
    research_gaps:            list[str]     # lacunas detectadas
    optimization_efficiency:  float         # 0–100: eficiência do plano
    computed_at:              str

    def to_dict(self) -> dict:
        return asdict(self)


class AutonomousResearchEvolution:
    """
    FASE 6 (Phase O): Extensão do AutonomousResearchScheduler com auto-direcionamento.

    O sistema decide autonomamente:
      - Quais estratégias merecem mais replay (via ResearchPrioritizer)
      - Quais cenários precisam mais testes (via fragilidade em cenários)
      - Quais datasets precisam validação (via drift de mercado)
      - Quais parâmetros precisam sweep (via ParameterIntelligence)
      - Quais gaps de research existem

    Reutiliza: ResearchPrioritizer, ParameterIntelligenceFleet, MarketDriftIntelligence.
    NÃO reimplementa scoring ou replay.
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def generate_plan(self, strategy_ids: list[str] | None = None) -> ResearchEvolutionPlan:
        """Gera plano de evolução de research autonomamente."""
        if not strategy_ids:
            strategy_files = list(self.experiments_dir.glob("*.jsonl"))
            strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]

        # ── Research priorities (via ResearchPrioritizer) ─────────────────────
        from domains.crypto_coin.research.research_prioritizer import ResearchPrioritizer
        prio_report = ResearchPrioritizer(self.experiments_dir).generate()

        strategy_priorities: list[dict] = []
        for task in prio_report.tasks[:10]:  # top 10
            strategy_priorities.append({
                "strategy_id":   task.strategy_id,
                "priority":      task.priority,
                "task_type":     task.task_type,
                "reason":        task.reason,
                "suggested_cmd": task.suggested_action,
            })

        # ── Parameter sweep priorities ─────────────────────────────────────────
        from domains.crypto_coin.research.parameter_intelligence import ParameterIntelligenceFleet
        param_fleet   = ParameterIntelligenceFleet(self.experiments_dir)
        param_reports = param_fleet.analyze_all()

        parameters_to_sweep: list[dict] = []
        for pr in param_reports:
            for pa in sorted(pr.parameters_analyzed, key=lambda a: a.priority_for_sweep, reverse=True)[:2]:
                if pa.priority_for_sweep >= 50:
                    parameters_to_sweep.append({
                        "strategy_id":     pr.strategy_id,
                        "parameter":       pa.parameter_name,
                        "priority":        round(pa.priority_for_sweep, 1),
                        "is_fragile":      pa.is_fragile,
                        "recommended_range": list(pa.recommended_range) if pa.recommended_range else None,
                    })

        # ── Scenario priorities (degradadas têm prioridade em cenários adversos) ─
        from domains.crypto_coin.research.strategy_degradation_intelligence import DegradationFleetAnalyzer
        fleet_reports = DegradationFleetAnalyzer(self.experiments_dir).rank_all()
        degraded_ids  = [r.strategy_id for r in fleet_reports if r.composite_risk_score >= 50]

        scenarios_to_simulate: list[str] = []
        if degraded_ids:
            # Degradadas devem ser testadas nos cenários mais adversos
            scenarios_to_simulate = ["bear_market", "high_vol", "news_shock"]
        else:
            scenarios_to_simulate = ["bull_market", "sideways"]

        # ── Dataset validation priorities (via drift) ─────────────────────────
        drift_report = MarketDriftIntelligence(self.experiments_dir).analyze()
        datasets_to_validate: list[str] = []
        if drift_report.market_drift_score >= 40:
            datasets_to_validate = ["BTC/USDT:15m", "ETH/USDT:15m", "BTC/USDT:1h"]
        elif drift_report.edge_decay_score >= 30:
            datasets_to_validate = ["BTC/USDT:15m"]

        # ── Research gaps ─────────────────────────────────────────────────────
        gaps: list[str] = []
        if not strategy_ids:
            gaps.append("Nenhuma estratégia registrada — executar sweep_runner para criar dados")
        low_data = [r.strategy_id for r in fleet_reports if r.experiments_analyzed < 5]
        if low_data:
            gaps.append(f"Estratégias com poucos experimentos (<5): {', '.join(low_data[:3])}")
        if drift_report.market_drift_score >= 60:
            gaps.append("Drift elevado — replay out-of-sample recente necessário")
        if not parameters_to_sweep:
            gaps.append("Nenhum parâmetro prioritário identificado — dados de sweep insuficientes")

        # ── Optimization efficiency ───────────────────────────────────────────
        tasks_scored   = len([t for t in prio_report.tasks if t.priority in ("critical", "high")])
        plan_size      = len(strategy_priorities) + len(parameters_to_sweep)
        opt_efficiency = min(100.0, (plan_size / max(len(strategy_ids), 1)) * 50 + tasks_scored * 5)

        return ResearchEvolutionPlan(
            strategy_priorities     = strategy_priorities,
            scenarios_to_simulate   = scenarios_to_simulate,
            parameters_to_sweep     = parameters_to_sweep,
            datasets_to_validate    = datasets_to_validate,
            research_gaps           = gaps,
            optimization_efficiency = round(opt_efficiency, 1),
            computed_at             = datetime.now(timezone.utc).isoformat(),
        )

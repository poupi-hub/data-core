"""
research_prioritizer.py — Phase N FASE 6

Autonomous Research Prioritization.

Decide automaticamente quais estratégias merecem mais replay, quais cenários
precisam mais testes, e quais datasets precisam validação.

Scores produzidos:
  - research_priority_score:   urgência geral de research (0–100)
  - replay_priority_score:     urgência de novo replay (0–100)
  - validation_priority_score: urgência de validação OOS (0–100)

Saída: fila de tarefas de research priorizada para o operador.

Princípio anti-duplicação:
  Reutiliza StrategyDegradationIntelligence, FragilityIntelligenceAnalyzer e
  ExperimentTracker. NÃO reimplementa scoring ou replay.

CLI:
  python -m domains.crypto_coin.research.research_prioritizer
  python -m domains.crypto_coin.research.research_prioritizer --top 5
  python -m domains.crypto_coin.research.research_prioritizer --json
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
from domains.crypto_coin.research.strategy_degradation_intelligence import (
    StrategyDegradationIntelligence,
    DegradationFleetAnalyzer,
)
from domains.crypto_coin.research.fragility_intelligence import FragilityIntelligenceAnalyzer

EXPERIMENTS_DIR = Path("data/experiments")

# Prometheus (optional)
try:
    from api.metrics import research_priority_score as _prom_priority
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ResearchTask:
    """Tarefa de research priorizada."""
    task_id:          str
    strategy_id:      str
    task_type:        str   # replay | sweep | oos_validation | scenario_test | dataset_check
    priority:         str   # critical | high | medium | low
    priority_score:   float # 0–100
    reason:           str
    suggested_action: str
    estimated_effort: str   # low | medium | high


@dataclass
class ResearchPriorityReport:
    """Relatório de priorização de research para toda a frota."""
    tasks:                    list[ResearchTask]
    strategies_analyzed:      int
    critical_tasks:           int
    high_tasks:               int
    fleet_research_urgency:   float   # 0–100: quão urgente é executar research agora
    recommendation:           str
    computed_at:              str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tasks"] = [asdict(t) for t in self.tasks]
        return d


# ── Prioritizer ────────────────────────────────────────────────────────────────

class ResearchPrioritizer:
    """
    FASE 6: Prioriza tarefas de research com base em degradação, fragilidade
    e volume de experimentos.

    Critérios de priorização:
      replay_priority:     degradação alta + poucos experimentos recentes
      validation_priority: overfitting suspeito + sem dados OOS
      sweep_priority:      fragilidade alta + range de sweep estreito
      dataset_priority:    integridade baixa de dataset (se disponível)
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def generate(self) -> ResearchPriorityReport:
        """Gera relatório priorizado de research para toda a frota."""
        fleet_analyzer = DegradationFleetAnalyzer(self.experiments_dir)
        fleet_reports  = fleet_analyzer.rank_all()

        if not fleet_reports:
            return ResearchPriorityReport(
                tasks=[], strategies_analyzed=0, critical_tasks=0, high_tasks=0,
                fleet_research_urgency=0.0,
                recommendation="Sem estratégias registradas. Execute sweep_runner primeiro.",
                computed_at=datetime.now(timezone.utc).isoformat(),
            )

        tasks: list[ResearchTask] = []

        for deg_report in fleet_reports:
            sid = deg_report.strategy_id

            # Fragilidade
            try:
                frag = FragilityIntelligenceAnalyzer(sid, self.experiments_dir).analyze()
                fragility_score = frag.fragility_score
                overfitting_score = frag.overfitting_score
                sweep_experiments = frag.sweep_experiments
            except Exception:
                fragility_score = 0.0
                overfitting_score = 0.0
                sweep_experiments = 0

            n_experiments = deg_report.experiments_analyzed

            # ── Replay priority ────────────────────────────────────────────────
            replay_score = self._compute_replay_priority(
                degradation_score  = deg_report.degradation_score,
                composite_risk     = deg_report.composite_risk_score,
                n_experiments      = n_experiments,
            )
            if replay_score >= 40:
                tasks.append(ResearchTask(
                    task_id          = f"replay_{sid}",
                    strategy_id      = sid,
                    task_type        = "replay",
                    priority         = self._score_to_priority(replay_score),
                    priority_score   = round(replay_score, 1),
                    reason           = (
                        f"Degradação {deg_report.degradation_score:.0f}/100, "
                        f"risco {deg_report.composite_risk_score:.0f}/100"
                    ),
                    suggested_action = f"replay_from_db(db, '{sid}', 'BTC/USDT', '15m', days=180)",
                    estimated_effort = "medium",
                ))

            # ── OOS validation priority ────────────────────────────────────────
            validation_score = self._compute_validation_priority(
                overfitting_score  = overfitting_score,
                n_experiments      = n_experiments,
            )
            if validation_score >= 40:
                tasks.append(ResearchTask(
                    task_id          = f"oos_{sid}",
                    strategy_id      = sid,
                    task_type        = "oos_validation",
                    priority         = self._score_to_priority(validation_score),
                    priority_score   = round(validation_score, 1),
                    reason           = f"Suspeita de overfitting (score={overfitting_score:.0f})",
                    suggested_action = f"Executar replay em período OOS não usado nos sweeps anteriores",
                    estimated_effort = "high",
                ))

            # ── Sweep priority ─────────────────────────────────────────────────
            sweep_score = self._compute_sweep_priority(
                fragility_score   = fragility_score,
                sweep_experiments = sweep_experiments,
                health_score      = deg_report.strategy_health_score,
            )
            if sweep_score >= 40:
                tasks.append(ResearchTask(
                    task_id          = f"sweep_{sid}",
                    strategy_id      = sid,
                    task_type        = "sweep",
                    priority         = self._score_to_priority(sweep_score),
                    priority_score   = round(sweep_score, 1),
                    reason           = f"Fragilidade alta (score={fragility_score:.0f}), sweep estreito",
                    suggested_action = (
                        f"sweep_runner --strategy {sid} --symbol BTC/USDT --tf 15m "
                        "--sweep rsi_oversold:20,25,30,35,40 --sweep rsi_overbought:60,65,70,75,80"
                    ),
                    estimated_effort = "high",
                ))

        # Ordena por priority_score DESC
        tasks.sort(key=lambda t: t.priority_score, reverse=True)

        critical_tasks = sum(1 for t in tasks if t.priority == "critical")
        high_tasks     = sum(1 for t in tasks if t.priority == "high")

        fleet_urgency = statistics.mean(t.priority_score for t in tasks) if tasks else 0.0

        recommendation = self._fleet_recommendation(fleet_urgency, critical_tasks)

        # Emite métrica
        if _METRICS_AVAILABLE:
            try:
                _prom_priority.set(fleet_urgency)
            except Exception:
                pass

        return ResearchPriorityReport(
            tasks                  = tasks,
            strategies_analyzed    = len(fleet_reports),
            critical_tasks         = critical_tasks,
            high_tasks             = high_tasks,
            fleet_research_urgency = round(fleet_urgency, 1),
            recommendation         = recommendation,
            computed_at            = datetime.now(timezone.utc).isoformat(),
        )

    # ── Score computations ────────────────────────────────────────────────────

    def _compute_replay_priority(
        self,
        degradation_score: float,
        composite_risk:    float,
        n_experiments:     int,
    ) -> float:
        """Replay urgente quando degradação alta + poucos experimentos recentes."""
        degradation_component = degradation_score * 0.5
        risk_component        = composite_risk * 0.3
        data_gap_component    = max(0.0, 20.0 - min(20.0, n_experiments)) * 1.0  # até +20 pts
        return min(100.0, degradation_component + risk_component + data_gap_component)

    def _compute_validation_priority(
        self,
        overfitting_score: float,
        n_experiments:     int,
    ) -> float:
        """Validação OOS urgente quando overfitting suspeito."""
        base = overfitting_score * 0.7
        # Menos experimentos = mais urgente validar (menos evidência)
        data_gap = max(0.0, 15.0 - min(15.0, n_experiments)) * 1.0
        return min(100.0, base + data_gap)

    def _compute_sweep_priority(
        self,
        fragility_score:   float,
        sweep_experiments: int,
        health_score:      float,
    ) -> float:
        """Sweep urgente quando fragilidade alta + sweep estreito."""
        base      = fragility_score * 0.6
        data_gap  = max(0.0, 10.0 - min(10.0, sweep_experiments)) * 2.0  # até +20 pts
        health_gap = max(0.0, 60.0 - health_score) * 0.3   # saúde baixa aumenta urgência
        return min(100.0, base + data_gap + health_gap)

    def _score_to_priority(self, score: float) -> str:
        if score >= 75:
            return "critical"
        if score >= 55:
            return "high"
        if score >= 35:
            return "medium"
        return "low"

    def _fleet_recommendation(self, urgency: float, critical_tasks: int) -> str:
        if critical_tasks > 0:
            return f"{critical_tasks} tarefa(s) crítica(s) de research. Executar imediatamente antes do próximo ciclo de alocação."
        if urgency >= 60:
            return "Research urgente — múltiplas estratégias precisam de replay ou validação."
        if urgency >= 35:
            return "Research recomendado — priorizar sweep e replay das estratégias com maior risco."
        return "Research em dia — executar ciclo de manutenção regular."


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Research Prioritizer — Phase N FASE 6"
    )
    parser.add_argument("--top",  type=int, default=10, help="Mostrar top N tarefas")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    prioritizer = ResearchPrioritizer()
    report      = prioritizer.generate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nResearch Priority Report — {report.strategies_analyzed} estratégias")
    print(f"  fleet_research_urgency: {report.fleet_research_urgency:.0f}/100")
    print(f"  critical_tasks:         {report.critical_tasks}")
    print(f"  high_tasks:             {report.high_tasks}")
    print(f"  total_tasks:            {len(report.tasks)}")
    print(f"\n  → {report.recommendation}")

    if report.tasks:
        top_tasks = report.tasks[: args.top]
        print(f"\nTop {len(top_tasks)} tarefas:")
        print(f"{'Tipo':<16} {'Estratégia':<25} {'Prioridade':<10} {'Score':>6}  Razão")
        print("-" * 85)
        for t in top_tasks:
            print(
                f"{t.task_type:<16} {t.strategy_id:<25} {t.priority:<10} "
                f"{t.priority_score:>6.0f}  {t.reason}"
            )


if __name__ == "__main__":
    main()

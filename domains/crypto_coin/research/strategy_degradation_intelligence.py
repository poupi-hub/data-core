"""
strategy_degradation_intelligence.py — Phase M FASE 10

Inteligência de degradação adaptativa de estratégias.

Complementa strategy_intelligence.py (Phase L) com:
  - degradation_score:    índice numérico 0–100 de quão degradada está a estratégia
  - strategy_health_score: saúde geral da estratégia (0–100, inverso de risco)
  - stability_score:       estabilidade de performance entre períodos diferentes
  - robustness_score:      robustez a mudanças de parâmetros e cenários

Diferença da Phase L:
  strategy_intelligence.py  → detecta (sim/não), emite Prometheus
  strategy_degradation_intelligence.py → quantifica e gera scores para ranking adaptativo

CLI:
  python -m domains.crypto_coin.research.strategy_degradation_intelligence --strategy trend_following
  python -m domains.crypto_coin.research.strategy_degradation_intelligence --all --json
  python -m domains.crypto_coin.research.strategy_degradation_intelligence --rank
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.experiment_tracker import ExperimentTracker

try:
    from api.metrics import (
        strategy_degradation_score,
        strategy_health_score,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constantes ────────────────────────────────────────────────────────────────

EXPERIMENTS_DIR = Path("data/experiments")

# Thresholds para cálculo de scores
DEGRADATION_SHARPE_DROP_CRITICAL  = 0.30   # queda > 30% = crítico
DEGRADATION_SHARPE_DROP_HIGH      = 0.20   # queda > 20% = alto
STABILITY_STD_THRESHOLD           = 0.5    # std de sharpe entre períodos
ROBUSTNESS_BEST_WORST_THRESHOLD   = 3.0    # ratio best/worst sharpe em sweep


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DegradationIntelligenceReport:
    strategy_id:          str
    degradation_score:    float   # 0–100 (0 = sem degradação, 100 = totalmente degradado)
    strategy_health_score: float  # 0–100 (100 = saudável)
    stability_score:      float   # 0–100 (100 = muito estável)
    robustness_score:     float   # 0–100 (100 = muito robusto)
    composite_risk_score: float   # 0–100 (média ponderada dos riscos)

    # Métricas de suporte
    experiments_analyzed: int
    sharpe_recent:        float | None
    sharpe_prior:         float | None
    sharpe_std:           float | None
    sharpe_best:          float | None
    sharpe_worst:         float | None

    # Sinais textuais
    signals:         list[str]
    recommendation:  str
    evaluated_at:    str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Analyzer ──────────────────────────────────────────────────────────────────

class StrategyDegradationIntelligence:
    """
    Calcula scores quantitativos de saúde e degradação para uma estratégia.
    Lê experimentos do ExperimentTracker (JSONL).
    """

    def __init__(self, strategy_id: str, experiments_dir: Path = EXPERIMENTS_DIR):
        self.strategy_id      = strategy_id
        self.tracker          = ExperimentTracker(strategy_id=strategy_id,
                                                   experiments_dir=experiments_dir)

    def analyze(self) -> DegradationIntelligenceReport:
        """Executa análise completa e retorna o relatório de degradação."""
        experiments = self.tracker.load_experiments()

        if not experiments:
            return self._empty_report("Nenhum experimento registrado")

        sharpes = [e.metrics.get("sharpe", 0.0) for e in experiments if "sharpe" in e.metrics]

        # ── Degradation Score ───────────────────────────────────────────────
        degradation_score, sharpe_recent, sharpe_prior = self._compute_degradation_score(experiments)

        # ── Stability Score ─────────────────────────────────────────────────
        stability_score, sharpe_std = self._compute_stability_score(sharpes)

        # ── Robustness Score ────────────────────────────────────────────────
        robustness_score, sharpe_best, sharpe_worst = self._compute_robustness_score(experiments)

        # ── Strategy Health Score (inverso de degradação + robustez) ────────
        strategy_health_score = round(
            (100 - degradation_score) * 0.4 +
            stability_score           * 0.3 +
            robustness_score          * 0.3,
            1
        )

        # ── Composite Risk Score ─────────────────────────────────────────────
        composite_risk_score = round(
            degradation_score  * 0.5 +
            (100 - stability_score)  * 0.25 +
            (100 - robustness_score) * 0.25,
            1
        )

        # ── Sinais textuais ──────────────────────────────────────────────────
        signals: list[str] = []
        if degradation_score >= 70:
            signals.append(f"CRITICO: degradação severa (score={degradation_score:.0f})")
        elif degradation_score >= 40:
            signals.append(f"ALERTA: degradação moderada (score={degradation_score:.0f})")

        if stability_score < 40:
            signals.append(f"Instabilidade alta entre períodos (sharpe_std={sharpe_std:.2f})")

        if robustness_score < 40:
            signals.append(f"Fragilidade detectada (best/worst ratio={sharpe_best/(sharpe_worst or 1e-9):.1f}x)")

        # ── Recomendação ─────────────────────────────────────────────────────
        if composite_risk_score >= 70:
            recommendation = "Suspender estratégia — risco composto crítico. Requer revisão completa."
        elif composite_risk_score >= 40:
            recommendation = "Monitorar de perto — risco moderado. Executar novo sweep com parâmetros conservadores."
        else:
            recommendation = "Estratégia saudável — continuar monitoramento regular."

        report = DegradationIntelligenceReport(
            strategy_id           = self.strategy_id,
            degradation_score     = degradation_score,
            strategy_health_score = strategy_health_score,
            stability_score       = stability_score,
            robustness_score      = robustness_score,
            composite_risk_score  = composite_risk_score,
            experiments_analyzed  = len(experiments),
            sharpe_recent         = sharpe_recent,
            sharpe_prior          = sharpe_prior,
            sharpe_std            = sharpe_std,
            sharpe_best           = sharpe_best,
            sharpe_worst          = sharpe_worst,
            signals               = signals,
            recommendation        = recommendation,
            evaluated_at          = datetime.now(timezone.utc).isoformat(),
        )

        # Emite métricas Prometheus
        if _METRICS_AVAILABLE:
            try:
                strategy_degradation_score.labels(strategy_id=self.strategy_id).set(degradation_score)
                strategy_health_score.labels(strategy_id=self.strategy_id).set(strategy_health_score)
            except Exception:
                pass

        return report

    # ── Scores internos ───────────────────────────────────────────────────────

    def _compute_degradation_score(
        self, experiments: list[Any]
    ) -> tuple[float, float | None, float | None]:
        """
        Degradation score 0–100 baseado na queda de sharpe no tempo.
        Ordena experimentos por created_at e compara primeira/segunda metade.
        """
        sorted_exps = sorted(experiments, key=lambda e: e.created_at)
        if len(sorted_exps) < 4:
            return 0.0, None, None

        mid = len(sorted_exps) // 2
        prior_sharpes  = [e.metrics.get("sharpe", 0.0) for e in sorted_exps[:mid]]
        recent_sharpes = [e.metrics.get("sharpe", 0.0) for e in sorted_exps[mid:]]

        prior_avg  = statistics.mean(prior_sharpes)  if prior_sharpes  else 0.0
        recent_avg = statistics.mean(recent_sharpes) if recent_sharpes else 0.0

        if prior_avg <= 0:
            return 0.0, round(recent_avg, 3), round(prior_avg, 3)

        drop_pct = (prior_avg - recent_avg) / abs(prior_avg)   # positivo = degradação

        if drop_pct >= DEGRADATION_SHARPE_DROP_CRITICAL:
            score = min(100.0, 70.0 + (drop_pct - DEGRADATION_SHARPE_DROP_CRITICAL) * 100)
        elif drop_pct >= DEGRADATION_SHARPE_DROP_HIGH:
            score = 40.0 + (drop_pct - DEGRADATION_SHARPE_DROP_HIGH) * 150
        elif drop_pct > 0:
            score = drop_pct * 200   # 10% queda → 20 pts
        else:
            score = 0.0  # melhorando

        return round(min(100.0, max(0.0, score)), 1), round(recent_avg, 3), round(prior_avg, 3)

    def _compute_stability_score(self, sharpes: list[float]) -> tuple[float, float | None]:
        """
        Stability score 0–100 baseado no desvio padrão do sharpe entre todos os experimentos.
        Baixo std → alta estabilidade.
        """
        if len(sharpes) < 3:
            return 75.0, None

        try:
            std = statistics.stdev(sharpes)
        except statistics.StatisticsError:
            return 75.0, None

        # std < 0.2 = muito estável (100), std > 1.0 = muito instável (0)
        score = max(0.0, 100.0 - (std / STABILITY_STD_THRESHOLD) * 50.0)
        return round(min(100.0, score), 1), round(std, 3)

    def _compute_robustness_score(
        self, experiments: list[Any]
    ) -> tuple[float, float | None, float | None]:
        """
        Robustness score 0–100 baseado na razão best/worst sharpe nos experimentos de sweep.
        Razão alta → fragilidade → baixa robustez.
        """
        sweep_exps = [e for e in experiments if any("sweep" in str(t) for t in getattr(e, "tags", []))]
        if len(sweep_exps) < 3:
            # Sem dados de sweep — usa todos os experimentos
            sweep_exps = experiments

        sharpes = [e.metrics.get("sharpe", 0.0) for e in sweep_exps if "sharpe" in e.metrics]
        if len(sharpes) < 2:
            return 75.0, None, None

        best  = max(sharpes)
        worst = min(sharpes)

        if worst <= 0 or best <= 0:
            return 50.0, round(best, 3), round(worst, 3)

        ratio = best / worst
        # ratio < 1.5 = robusto (100), ratio > 5 = frágil (0)
        score = max(0.0, 100.0 - (ratio / ROBUSTNESS_BEST_WORST_THRESHOLD) * 50.0)
        return round(min(100.0, score), 1), round(best, 3), round(worst, 3)

    def _empty_report(self, reason: str) -> DegradationIntelligenceReport:
        return DegradationIntelligenceReport(
            strategy_id=self.strategy_id,
            degradation_score=0.0, strategy_health_score=50.0,
            stability_score=50.0, robustness_score=50.0,
            composite_risk_score=0.0, experiments_analyzed=0,
            sharpe_recent=None, sharpe_prior=None, sharpe_std=None,
            sharpe_best=None, sharpe_worst=None,
            signals=[reason], recommendation="Executar experimentos para habilitar análise",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


# ── Fleet analysis ────────────────────────────────────────────────────────────

class DegradationFleetAnalyzer:
    """Analisa todas as estratégias registradas e rankeia por risco."""

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def rank_all(self) -> list[DegradationIntelligenceReport]:
        """Rankeia todas as estratégias por composite_risk_score (maior = mais risco)."""
        strategy_files = list(self.experiments_dir.glob("*.jsonl"))
        strategy_ids = [
            f.stem for f in strategy_files
            if f.stem != "all_experiments"
        ]

        if not strategy_ids:
            return []

        reports = []
        for sid in strategy_ids:
            try:
                analyzer = StrategyDegradationIntelligence(sid, self.experiments_dir)
                reports.append(analyzer.analyze())
            except Exception as e:
                print(f"[WARN] Erro ao analisar {sid}: {e}")

        return sorted(reports, key=lambda r: r.composite_risk_score, reverse=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Strategy Degradation Intelligence — Phase M FASE 10")
    parser.add_argument("--strategy", help="strategy_id específico")
    parser.add_argument("--all",  action="store_true", help="Analisa todas as estratégias")
    parser.add_argument("--rank", action="store_true", help="Ranking por risco (mais alto = mais risco)")
    parser.add_argument("--json", action="store_true", help="Output em JSON")
    args = parser.parse_args()

    if args.strategy:
        analyzer = StrategyDegradationIntelligence(args.strategy)
        report = analyzer.analyze()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\nDegradation Intelligence — {report.strategy_id}")
            print(f"  health_score:     {report.strategy_health_score:.0f}/100")
            print(f"  degradation:      {report.degradation_score:.0f}/100")
            print(f"  stability:        {report.stability_score:.0f}/100")
            print(f"  robustness:       {report.robustness_score:.0f}/100")
            print(f"  composite_risk:   {report.composite_risk_score:.0f}/100")
            print(f"  experiments:      {report.experiments_analyzed}")
            if report.signals:
                print("  signals:")
                for s in report.signals:
                    print(f"    - {s}")
            print(f"  recommendation:   {report.recommendation}")

    elif args.all or args.rank:
        fleet = DegradationFleetAnalyzer()
        reports = fleet.rank_all()
        if args.json:
            print(json.dumps([r.to_dict() for r in reports], indent=2))
        else:
            print(f"\nDegradation Fleet Ranking ({len(reports)} estratégias)")
            print(f"{'Strategy':<25} {'Health':>7} {'Risk':>6} {'Degrad':>7} {'Stability':>10} {'Robust':>8}")
            print("-" * 70)
            for r in reports:
                print(
                    f"{r.strategy_id:<25} {r.strategy_health_score:>7.0f} "
                    f"{r.composite_risk_score:>6.0f} {r.degradation_score:>7.0f} "
                    f"{r.stability_score:>10.0f} {r.robustness_score:>8.0f}"
                )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

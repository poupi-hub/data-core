"""
fragility_intelligence.py — Phase M FASE 11

Inteligência de overfitting e fragilidade de estratégias quantitativas.

Expande a detecção binária de Phase L para scores quantitativos:
  - fragility_score:          fragilidade de parâmetros (0–100, 100 = muito frágil)
  - overfitting_score:        risco de overfitting (0–100)
  - replay_consistency_score: consistência entre diferentes replays (0–100)
  - perturbation_sensitivity: sensibilidade a pequenas mudanças de parâmetro

Perturbation tests:
  Simula variações ±10% nos parâmetros do melhor experimento e verifica
  quão estável é o sharpe. Sem re-execução real — usa experiments históricos
  como proxy de perturbação.

CLI:
  python -m domains.crypto_coin.research.fragility_intelligence --strategy trend_following
  python -m domains.crypto_coin.research.fragility_intelligence --all
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

EXPERIMENTS_DIR = Path("data/experiments")

# Thresholds
OVERFIT_SHARPE_THRESHOLD       = 3.5   # sharpe > 3.5 = suspeito de overfit
OVERFIT_CONSISTENCY_RATE       = 0.8   # >80% dos runs com sharpe alto = suspeito
FRAGILITY_SHARPE_STD_HIGH      = 0.8   # std > 0.8 = alta fragilidade
FRAGILITY_SHARPE_STD_MODERATE  = 0.4   # std > 0.4 = fragilidade moderada


@dataclass
class FragilityIntelligenceReport:
    strategy_id:               str
    fragility_score:           float   # 0–100
    overfitting_score:         float   # 0–100
    replay_consistency_score:  float   # 0–100
    perturbation_sensitivity:  float   # 0–100 (100 = muito sensível)

    # Métricas de suporte
    sweep_experiments:         int
    sharpe_std:                float | None
    sharpe_mean:               float | None
    high_sharpe_rate:          float | None
    consistency_rate:          float | None   # % runs no mesmo timeframe com sharpe similar

    signals:                   list[str]
    recommendation:            str
    evaluated_at:              str

    def to_dict(self) -> dict:
        return asdict(self)


class FragilityIntelligenceAnalyzer:
    """
    Quantifica overfitting e fragilidade de uma estratégia.
    """

    def __init__(self, strategy_id: str, experiments_dir: Path = EXPERIMENTS_DIR):
        self.strategy_id = strategy_id
        self.tracker     = ExperimentTracker(strategy_id=strategy_id,
                                              experiments_dir=experiments_dir)

    def analyze(self) -> FragilityIntelligenceReport:
        experiments = self.tracker.load_experiments()

        if not experiments:
            return self._empty_report("Sem experimentos")

        sharpes = [e.metrics.get("sharpe", 0.0) for e in experiments if "sharpe" in e.metrics]

        # ── Fragility Score ─────────────────────────────────────────────────
        fragility_score, sharpe_std = self._compute_fragility_score(experiments, sharpes)

        # ── Overfitting Score ───────────────────────────────────────────────
        overfitting_score, high_sharpe_rate = self._compute_overfitting_score(sharpes)

        # ── Replay Consistency Score ────────────────────────────────────────
        replay_consistency_score, consistency_rate = self._compute_replay_consistency(experiments)

        # ── Perturbation Sensitivity ────────────────────────────────────────
        perturbation_sensitivity = self._compute_perturbation_sensitivity(experiments)

        # ── Sinais ────────────────────────────────────────────────────────────
        signals: list[str] = []
        if overfitting_score >= 70:
            signals.append(f"CRITICO: overfitting suspeito (sharpe médio={statistics.mean(sharpes):.2f})")
        if fragility_score >= 70:
            signals.append(f"ALERTA: fragilidade de parâmetros alta (std={sharpe_std:.2f})")
        if replay_consistency_score < 40:
            signals.append("Inconsistência entre replays diferentes — dados ou período instável")
        if perturbation_sensitivity >= 70:
            signals.append("Alta sensibilidade a perturbações — estratégia frágil")

        # ── Recomendação ─────────────────────────────────────────────────────
        max_risk = max(fragility_score, overfitting_score)
        if max_risk >= 70:
            recommendation = "Estratégia provavelmente overfittada/frágil — não usar em produção sem validação out-of-sample"
        elif max_risk >= 40:
            recommendation = "Risco moderado — executar sweep mais amplo e validar em período diferente"
        else:
            recommendation = "Fragilidade dentro do aceitável — continuar monitoramento"

        return FragilityIntelligenceReport(
            strategy_id              = self.strategy_id,
            fragility_score          = fragility_score,
            overfitting_score        = overfitting_score,
            replay_consistency_score = replay_consistency_score,
            perturbation_sensitivity = perturbation_sensitivity,
            sweep_experiments        = len([e for e in experiments
                                             if any("sweep" in str(t) for t in getattr(e, "tags", []))]),
            sharpe_std               = sharpe_std,
            sharpe_mean              = round(statistics.mean(sharpes), 3) if sharpes else None,
            high_sharpe_rate         = high_sharpe_rate,
            consistency_rate         = consistency_rate,
            signals                  = signals,
            recommendation           = recommendation,
            evaluated_at             = datetime.now(timezone.utc).isoformat(),
        )

    def _compute_fragility_score(
        self, experiments: list[Any], sharpes: list[float]
    ) -> tuple[float, float | None]:
        """Fragilidade = desvio padrão do sharpe entre experimentos de sweep."""
        sweep_exps = [e for e in experiments
                      if any("sweep" in str(t) for t in getattr(e, "tags", []))]
        target = sweep_exps if len(sweep_exps) >= 3 else experiments

        sharpes_t = [e.metrics.get("sharpe", 0.0) for e in target if "sharpe" in e.metrics]
        if len(sharpes_t) < 2:
            return 25.0, None

        try:
            std = statistics.stdev(sharpes_t)
        except statistics.StatisticsError:
            return 25.0, None

        if std >= FRAGILITY_SHARPE_STD_HIGH:
            score = min(100.0, 70.0 + (std - FRAGILITY_SHARPE_STD_HIGH) * 30.0)
        elif std >= FRAGILITY_SHARPE_STD_MODERATE:
            score = 40.0 + (std - FRAGILITY_SHARPE_STD_MODERATE) * 75.0
        else:
            score = std * 100.0

        return round(score, 1), round(std, 3)

    def _compute_overfitting_score(
        self, sharpes: list[float]
    ) -> tuple[float, float | None]:
        """Overfitting = muitos runs com sharpe anormalmente alto."""
        if not sharpes:
            return 0.0, None

        high_rate = sum(1 for s in sharpes if s > OVERFIT_SHARPE_THRESHOLD) / len(sharpes)
        mean_sharpe = statistics.mean(sharpes)

        # Combinação: taxa de sharpe alto + magnitude do sharpe médio
        score = 0.0
        if high_rate >= OVERFIT_CONSISTENCY_RATE:
            score += 60.0
        elif high_rate >= 0.5:
            score += 30.0
        elif high_rate >= 0.2:
            score += 10.0

        if mean_sharpe > 4.0:
            score += 40.0
        elif mean_sharpe > OVERFIT_SHARPE_THRESHOLD:
            score += 20.0

        return round(min(100.0, score), 1), round(high_rate, 3)

    def _compute_replay_consistency(
        self, experiments: list[Any]
    ) -> tuple[float, float | None]:
        """
        Consistência entre replays diferentes do mesmo símbolo.
        Experimentos com mesmo símbolo e timeframe deveriam ter sharpe similar.
        """
        from collections import defaultdict

        groups: dict[str, list[float]] = defaultdict(list)
        for e in experiments:
            key = f"{e.symbol}:{e.timeframe}"
            if "sharpe" in e.metrics:
                groups[key].append(e.metrics["sharpe"])

        if not groups or all(len(v) < 2 for v in groups.values()):
            return 75.0, None

        # Coeficiente de variação médio entre grupos
        cvs: list[float] = []
        for sharpe_list in groups.values():
            if len(sharpe_list) >= 2:
                mean = statistics.mean(sharpe_list)
                if mean > 0:
                    try:
                        std  = statistics.stdev(sharpe_list)
                        cvs.append((std / mean) * 100)
                    except statistics.StatisticsError:
                        pass

        if not cvs:
            return 75.0, None

        avg_cv = statistics.mean(cvs)
        # CV < 10% = muito consistente (100), CV > 50% = inconsistente (0)
        score = max(0.0, 100.0 - avg_cv * 2.0)
        consistency_rate = 1.0 - (avg_cv / 100.0)
        return round(min(100.0, score), 1), round(consistency_rate, 3)

    def _compute_perturbation_sensitivity(self, experiments: list[Any]) -> float:
        """
        Proxy de sensibilidade a perturbações:
        usa variação do sharpe em experimentos com parâmetros similares.
        Sem re-execução real — analisa experimentos históricos próximos.
        """
        if len(experiments) < 4:
            return 25.0   # poucos dados = assume sensibilidade moderada

        # Agrupa por número de parâmetros (proxy de complexidade)
        n_params = [len(e.parameters) for e in experiments]
        avg_params = statistics.mean(n_params)

        sharpes = [e.metrics.get("sharpe", 0.0) for e in experiments if "sharpe" in e.metrics]
        if len(sharpes) < 2:
            return 25.0

        try:
            std = statistics.stdev(sharpes)
        except statistics.StatisticsError:
            return 25.0

        # Alta complexidade + alta variância = alta sensibilidade
        complexity_factor = min(2.0, avg_params / 4.0)  # normalizado para 4 parâmetros
        sensitivity = min(100.0, std * 50.0 * complexity_factor)
        return round(sensitivity, 1)

    def _empty_report(self, reason: str) -> FragilityIntelligenceReport:
        return FragilityIntelligenceReport(
            strategy_id=self.strategy_id,
            fragility_score=0.0, overfitting_score=0.0,
            replay_consistency_score=75.0, perturbation_sensitivity=0.0,
            sweep_experiments=0, sharpe_std=None, sharpe_mean=None,
            high_sharpe_rate=None, consistency_rate=None,
            signals=[reason], recommendation="Executar experimentos e sweeps primeiro",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fragility Intelligence — Phase M FASE 11")
    parser.add_argument("--strategy", help="strategy_id específico")
    parser.add_argument("--all", action="store_true", help="Todas as estratégias")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    strategy_ids: list[str] = []
    if args.strategy:
        strategy_ids = [args.strategy]
    elif args.all:
        strategy_ids = [f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"]

    if not strategy_ids:
        parser.print_help()
        return

    for sid in strategy_ids:
        analyzer = FragilityIntelligenceAnalyzer(sid)
        report   = analyzer.analyze()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\nFragility Intelligence — {report.strategy_id}")
            print(f"  fragility:        {report.fragility_score:.0f}/100")
            print(f"  overfitting:      {report.overfitting_score:.0f}/100")
            print(f"  replay_consist:   {report.replay_consistency_score:.0f}/100")
            print(f"  perturbation:     {report.perturbation_sensitivity:.0f}/100")
            for s in report.signals:
                print(f"  ⚠ {s}")
            print(f"  → {report.recommendation}")


if __name__ == "__main__":
    main()

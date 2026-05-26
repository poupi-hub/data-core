"""
regime_aware_intelligence.py — Phase M FASE 12

Inteligência adaptiva de regime de mercado para estratégias quantitativas.

Complementa regime_analytics.py (Phase H) com:
  - regime_confidence_score:    confiança na classificação atual de regime (0–100)
  - regime_compatibility_matrix: qual estratégia funciona em qual regime
  - adaptive_regime_ranking:    ranking adaptativo de estratégias para o regime atual

Não executa live trading. Não altera posições reais.
Gera recomendações para o operador humano confirmar.

CLI:
  python -m domains.crypto_coin.research.regime_aware_intelligence --strategies trend_following breakout
  python -m domains.crypto_coin.research.regime_aware_intelligence --current-regime bull_market --strategies trend_following
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

# Regimes conhecidos (de scenario_runner.py)
KNOWN_REGIMES = ["bull_market", "bear_market", "sideways", "high_vol", "news_shock", "post_halving"]


@dataclass
class RegimeCompatibility:
    """Compatibilidade de uma estratégia com um regime."""
    strategy_id:    str
    regime:         str
    avg_sharpe:     float   # sharpe médio neste regime
    data_points:    int     # número de experimentos neste regime
    confidence:     str     # high/medium/low
    compatible:     bool    # sharpe > 0.5 = compatível


@dataclass
class RegimeAwareReport:
    strategy_id:              str
    regime_confidence_score:  float   # 0–100 (quanto confiar no regime_performance registrado)
    regimes_covered:          int     # quantos regimes têm dados
    compatibility_matrix:     list[RegimeCompatibility]
    best_regime:              str | None
    worst_regime:             str | None
    regime_sharpe_gap:        float   # diferença entre melhor e pior
    recommendation:           str
    evaluated_at:             str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["compatibility_matrix"] = [asdict(c) for c in self.compatibility_matrix]
        return d


@dataclass
class AdaptiveRegimeRanking:
    """Ranking de estratégias para o regime atual."""
    regime:               str
    ranked_strategies:    list[dict]   # [{strategy_id, sharpe, rank, recommended}]
    total_strategies:     int
    evaluated_at:         str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Analyzer ──────────────────────────────────────────────────────────────────

class RegimeAwareIntelligence:
    """
    Analisa compatibilidade de estratégias com regimes de mercado.
    Usa regime_performance registrado nos experimentos (Phase K+L).
    """

    def __init__(self, strategy_id: str, experiments_dir: Path = EXPERIMENTS_DIR):
        self.strategy_id = strategy_id
        self.tracker     = ExperimentTracker(strategy_id=strategy_id,
                                              experiments_dir=experiments_dir)

    def analyze(self) -> RegimeAwareReport:
        experiments = self.tracker.load_experiments()
        if not experiments:
            return self._empty_report("Sem experimentos")

        # Coleta regime_performance de todos os experimentos
        regime_data: dict[str, list[float]] = {r: [] for r in KNOWN_REGIMES}

        for exp in experiments:
            rp = getattr(exp, "regime_performance", {}) or {}
            for regime, perf in rp.items():
                sharpe = perf.get("sharpe", 0.0) if isinstance(perf, dict) else float(perf)
                if regime in regime_data:
                    regime_data[regime].append(sharpe)

        # Filtra regimes com dados
        covered = {r: vals for r, vals in regime_data.items() if vals}

        # Confidence score: baseado em cobertura e volume de dados
        total_data_points = sum(len(v) for v in covered.values())
        regimes_covered   = len(covered)
        confidence_score  = min(100.0, (regimes_covered / len(KNOWN_REGIMES)) * 50.0 +
                                       min(50.0, total_data_points * 5.0))

        # Compatibility matrix
        matrix: list[RegimeCompatibility] = []
        for regime, sharpes in covered.items():
            avg = statistics.mean(sharpes)
            n   = len(sharpes)
            conf = "high" if n >= 5 else "medium" if n >= 2 else "low"
            matrix.append(RegimeCompatibility(
                strategy_id = self.strategy_id,
                regime      = regime,
                avg_sharpe  = round(avg, 3),
                data_points = n,
                confidence  = conf,
                compatible  = avg >= 0.5,
            ))

        matrix.sort(key=lambda c: c.avg_sharpe, reverse=True)

        best_regime  = matrix[0].regime  if matrix else None
        worst_regime = matrix[-1].regime if matrix else None
        gap = (matrix[0].avg_sharpe - matrix[-1].avg_sharpe) if len(matrix) >= 2 else 0.0

        if gap > 2.0:
            recommendation = f"Estratégia instável entre regimes (gap={gap:.1f}) — evitar regime '{worst_regime}'"
        elif not matrix:
            recommendation = "Sem dados de regime — executar scenario_runner para cada regime"
        else:
            compatible_regimes = [c.regime for c in matrix if c.compatible]
            recommendation = f"Melhor em: {', '.join(compatible_regimes[:3]) if compatible_regimes else 'nenhum'}"

        return RegimeAwareReport(
            strategy_id             = self.strategy_id,
            regime_confidence_score = round(confidence_score, 1),
            regimes_covered         = regimes_covered,
            compatibility_matrix    = matrix,
            best_regime             = best_regime,
            worst_regime            = worst_regime,
            regime_sharpe_gap       = round(gap, 3),
            recommendation          = recommendation,
            evaluated_at            = datetime.now(timezone.utc).isoformat(),
        )

    def _empty_report(self, reason: str) -> RegimeAwareReport:
        return RegimeAwareReport(
            strategy_id=self.strategy_id, regime_confidence_score=0.0,
            regimes_covered=0, compatibility_matrix=[],
            best_regime=None, worst_regime=None, regime_sharpe_gap=0.0,
            recommendation=reason, evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


def rank_strategies_for_regime(
    strategy_ids: list[str],
    regime: str,
    experiments_dir: Path = EXPERIMENTS_DIR,
) -> AdaptiveRegimeRanking:
    """
    Rankeia estratégias para o regime especificado.
    Útil para adaptive allocation: "dado que estamos em bull_market, qual estratégia usar?"
    """
    ranked: list[dict] = []

    for sid in strategy_ids:
        try:
            analyzer = RegimeAwareIntelligence(sid, experiments_dir)
            report   = analyzer.analyze()
            regime_match = next(
                (c for c in report.compatibility_matrix if c.regime == regime), None
            )
            sharpe = regime_match.avg_sharpe if regime_match else 0.0
            ranked.append({
                "strategy_id": sid,
                "sharpe":      sharpe,
                "compatible":  regime_match.compatible if regime_match else False,
                "confidence":  regime_match.confidence if regime_match else "low",
            })
        except Exception as e:
            ranked.append({"strategy_id": sid, "sharpe": 0.0, "compatible": False, "confidence": "low"})

    ranked.sort(key=lambda x: x["sharpe"], reverse=True)
    for i, item in enumerate(ranked, 1):
        item["rank"] = i
        item["recommended"] = item["compatible"] and i <= 2

    return AdaptiveRegimeRanking(
        regime=regime,
        ranked_strategies=ranked,
        total_strategies=len(ranked),
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Regime-Aware Intelligence — Phase M FASE 12")
    parser.add_argument("--strategies", nargs="+", help="strategy_ids")
    parser.add_argument("--current-regime", help="Regime atual para ranking adaptativo")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    strategy_ids = args.strategies or [
        f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"
    ]

    if args.current_regime:
        ranking = rank_strategies_for_regime(strategy_ids, args.current_regime)
        if args.json:
            print(json.dumps(ranking.to_dict(), indent=2))
        else:
            print(f"\nAdaptive Regime Ranking — {args.current_regime}")
            for item in ranking.ranked_strategies:
                rec = "✓" if item.get("recommended") else " "
                print(f"  [{rec}] {item['rank']}. {item['strategy_id']:<25} sharpe={item['sharpe']:.3f} ({item['confidence']})")
        return

    for sid in strategy_ids:
        analyzer = RegimeAwareIntelligence(sid)
        report   = analyzer.analyze()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\nRegime Intelligence — {report.strategy_id}")
            print(f"  confidence_score: {report.regime_confidence_score:.0f}/100")
            print(f"  regimes_covered:  {report.regimes_covered}/{len(KNOWN_REGIMES)}")
            print(f"  best_regime:      {report.best_regime or 'N/A'}")
            print(f"  worst_regime:     {report.worst_regime or 'N/A'}")
            print(f"  sharpe_gap:       {report.regime_sharpe_gap:.2f}")
            print(f"  → {report.recommendation}")


if __name__ == "__main__":
    main()

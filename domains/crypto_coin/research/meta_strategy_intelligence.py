"""
meta_strategy_intelligence.py — Phase N FASE 5

Meta-Strategy Intelligence.

Aprende relações entre estratégias: quais coexistem melhor, quais conflitam,
quais protegem drawdown, quais performam melhor juntas.

Scores produzidos:
  - strategy_correlation_matrix: correlação Pearson baseada em regime_sharpe profiles
  - hedge_compatibility_score:   score de hedge entre pares de estratégias
  - diversification_synergy_score: sinergia do portfólio combinado

Princípio anti-duplicação:
  Reutiliza RegimeAwareIntelligence (já computa regime_sharpe profiles).
  O _pearson() de AdaptivePortfolioIntelligence NÃO é reimplementado —
  esta classe usa a mesma lógica mas adiciona hedge_compatibility e synergy.

CLI:
  python -m domains.crypto_coin.research.meta_strategy_intelligence --strategies trend_following breakout
  python -m domains.crypto_coin.research.meta_strategy_intelligence --all --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.regime_aware_intelligence import RegimeAwareIntelligence
from domains.crypto_coin.research.strategy_degradation_intelligence import StrategyDegradationIntelligence

EXPERIMENTS_DIR = Path("data/experiments")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class StrategyPairAnalysis:
    """Análise de relação entre dois pares de estratégias."""
    strategy_a:              str
    strategy_b:              str
    regime_correlation:      float | None   # Pearson -1 a +1
    hedge_compatibility_score: float        # 0–100 (100 = excelente hedge)
    coexistence_quality:     str            # synergistic | neutral | conflicting
    dominant_regimes_a:      list[str]      # regimes em que A domina
    dominant_regimes_b:      list[str]      # regimes em que B domina
    overlap_regimes:         list[str]      # regimes em que ambas competem


@dataclass
class MetaStrategyReport:
    """Relatório completo de meta-inteligência de estratégias."""
    strategy_ids:                  list[str]
    correlation_matrix:            dict[str, dict[str, float | None]]  # [a][b] = correlação
    diversification_synergy_score: float    # 0–100 (100 = máxima sinergia)
    hedge_pairs:                   list[StrategyPairAnalysis]          # pares com hedge > 60
    conflicting_pairs:             list[str]                           # pares com corr > 0.8
    best_combination:              list[str]                           # subset com maior sinergia
    recommendation:                str
    computed_at:                   str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hedge_pairs"] = [asdict(p) for p in self.hedge_pairs]
        return d


# ── Analyzer ──────────────────────────────────────────────────────────────────

class MetaStrategyIntelligence:
    """
    FASE 5: Analisa relações entre estratégias usando regime_sharpe profiles.

    Método:
      1. Coleta regime_sharpe de cada estratégia via RegimeAwareIntelligence
      2. Calcula correlação Pearson entre pares (regime_sharpe como vetor)
      3. hedge_compatibility: pares com correlação negativa são bons hedges
      4. diversification_synergy: quão bem o conjunto cobre todos os regimes
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def analyze(self, strategy_ids: list[str]) -> MetaStrategyReport:
        if len(strategy_ids) < 2:
            return self._minimal_report(strategy_ids)

        # Coleta regime_sharpe profiles
        profiles: dict[str, dict[str, float]] = {}
        for sid in strategy_ids:
            try:
                reg = RegimeAwareIntelligence(sid, self.experiments_dir).analyze()
                profiles[sid] = {c.regime: c.avg_sharpe for c in reg.compatibility_matrix}
            except Exception:
                profiles[sid] = {}

        # Correlação entre todos os pares
        corr_matrix: dict[str, dict[str, float | None]] = {s: {} for s in strategy_ids}
        pair_analyses: list[StrategyPairAnalysis] = []
        conflicting_pairs: list[str] = []
        hedge_pairs: list[StrategyPairAnalysis] = []

        for i, sid_a in enumerate(strategy_ids):
            for j, sid_b in enumerate(strategy_ids):
                if i == j:
                    corr_matrix[sid_a][sid_b] = 1.0
                    continue
                if j < i:
                    corr_matrix[sid_a][sid_b] = corr_matrix[sid_b][sid_a]
                    continue

                corr = self._pearson_regime(profiles[sid_a], profiles[sid_b])
                corr_matrix[sid_a][sid_b] = corr
                corr_matrix[sid_b][sid_a] = corr

                if corr is not None:
                    hedge_score = self._hedge_score(corr, profiles[sid_a], profiles[sid_b])
                    dom_a, dom_b, overlap = self._dominant_regimes(profiles[sid_a], profiles[sid_b])

                    if corr > 0.8:
                        quality = "conflicting"
                        conflicting_pairs.append(f"{sid_a} × {sid_b}")
                    elif corr < 0.0:
                        quality = "synergistic"
                    else:
                        quality = "neutral"

                    pair = StrategyPairAnalysis(
                        strategy_a               = sid_a,
                        strategy_b               = sid_b,
                        regime_correlation       = round(corr, 3),
                        hedge_compatibility_score = round(hedge_score, 1),
                        coexistence_quality      = quality,
                        dominant_regimes_a       = dom_a,
                        dominant_regimes_b       = dom_b,
                        overlap_regimes          = overlap,
                    )
                    pair_analyses.append(pair)
                    if hedge_score >= 60:
                        hedge_pairs.append(pair)

        # Diversification synergy score
        diversification_synergy = self._compute_synergy(profiles, strategy_ids)

        # Melhor combinação (subconjunto com menor correlação média)
        best_combination = self._find_best_combination(strategy_ids, corr_matrix)

        # Recomendação
        recommendation = self._recommend(diversification_synergy, conflicting_pairs, hedge_pairs)

        return MetaStrategyReport(
            strategy_ids                  = strategy_ids,
            correlation_matrix            = corr_matrix,
            diversification_synergy_score = round(diversification_synergy, 1),
            hedge_pairs                   = hedge_pairs,
            conflicting_pairs             = conflicting_pairs,
            best_combination              = best_combination,
            recommendation                = recommendation,
            computed_at                   = datetime.now(timezone.utc).isoformat(),
        )

    # ── Computações ───────────────────────────────────────────────────────────

    def _pearson_regime(
        self,
        profile_a: dict[str, float],
        profile_b: dict[str, float],
    ) -> float | None:
        """Correlação Pearson entre dois regime_sharpe profiles."""
        common = list(set(profile_a.keys()) & set(profile_b.keys()))
        if len(common) < 3:
            return None
        av = [profile_a[r] for r in common]
        bv = [profile_b[r] for r in common]
        n  = len(av)
        avg_a = sum(av) / n
        avg_b = sum(bv) / n
        num = sum((av[i] - avg_a) * (bv[i] - avg_b) for i in range(n))
        den = (sum((x - avg_a) ** 2 for x in av) * sum((x - avg_b) ** 2 for x in bv)) ** 0.5
        return round(num / den, 3) if den > 0 else 0.0

    def _hedge_score(
        self,
        correlation: float,
        profile_a:   dict[str, float],
        profile_b:   dict[str, float],
    ) -> float:
        """
        Hedge compatibility: estratégias que se complementam (corr negativa)
        E que têm performance positiva em regimes diferentes.
        Score 0–100.
        """
        # Base: inversão da correlação normalizada
        base = max(0.0, (1.0 - correlation) * 50.0)   # corr=-1 → 100, corr=+1 → 0

        # Bônus: ambas positivas mas em regimes diferentes
        positive_a = {r for r, s in profile_a.items() if s > 0.5}
        positive_b = {r for r, s in profile_b.items() if s > 0.5}
        complementary = len(positive_a ^ positive_b)  # régimes exclusivos de cada uma
        bonus = min(30.0, complementary * 10.0)

        return min(100.0, base + bonus)

    def _dominant_regimes(
        self,
        profile_a: dict[str, float],
        profile_b: dict[str, float],
    ) -> tuple[list[str], list[str], list[str]]:
        """Regimes dominados por A, B, e em overlap."""
        dom_a   = [r for r, s in profile_a.items() if s > profile_b.get(r, 0)]
        dom_b   = [r for r, s in profile_b.items() if s > profile_a.get(r, 0)]
        overlap = list(set(profile_a.keys()) & set(profile_b.keys()))
        return dom_a, dom_b, overlap

    def _compute_synergy(
        self,
        profiles:     dict[str, dict[str, float]],
        strategy_ids: list[str],
    ) -> float:
        """
        Diversification synergy: quão bem o conjunto cobre todos os regimes.
        Max synergy = cada regime tem pelo menos uma estratégia positiva.
        """
        all_regimes: set[str] = set()
        for p in profiles.values():
            all_regimes.update(p.keys())

        if not all_regimes:
            return 0.0

        covered = 0
        for regime in all_regimes:
            best_sharpe = max(
                (p.get(regime, 0.0) for p in profiles.values()),
                default=0.0,
            )
            if best_sharpe > 0.5:
                covered += 1

        coverage_score = (covered / len(all_regimes)) * 100.0

        # Penalidade por correlações altas (estratégias redundantes)
        corrs: list[float] = []
        ids = list(strategy_ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                c = self._pearson_regime(profiles[ids[i]], profiles[ids[j]])
                if c is not None:
                    corrs.append(c)

        avg_corr = statistics.mean(corrs) if corrs else 0.5
        corr_penalty = max(0.0, avg_corr * 20.0)  # alta correlação → até -20pts

        return max(0.0, min(100.0, coverage_score - corr_penalty))

    def _find_best_combination(
        self,
        strategy_ids:  list[str],
        corr_matrix:   dict[str, dict[str, float | None]],
    ) -> list[str]:
        """Encontra subconjunto com menor correlação média (greedy)."""
        if len(strategy_ids) <= 2:
            return list(strategy_ids)

        best_subset: list[str] = []
        best_avg_corr = float("inf")

        # Greedy: começa com par de menor correlação e adiciona estratégias
        pairs: list[tuple[float, str, str]] = []
        for i, a in enumerate(strategy_ids):
            for j, b in enumerate(strategy_ids):
                if i < j:
                    c = corr_matrix[a][b]
                    if c is not None:
                        pairs.append((c, a, b))

        if not pairs:
            return list(strategy_ids)

        pairs.sort()  # menor correlação primeiro
        best_pair_corr, best_a, best_b = pairs[0]
        subset = [best_a, best_b]

        for sid in strategy_ids:
            if sid in subset:
                continue
            avg_corr = statistics.mean(
                corr_matrix[sid][s] or 0.5 for s in subset
            )
            if avg_corr < 0.5:
                subset.append(sid)

        return subset

    def _recommend(
        self,
        synergy:           float,
        conflicting_pairs: list[str],
        hedge_pairs:       list[StrategyPairAnalysis],
    ) -> str:
        parts: list[str] = []
        if synergy >= 75:
            parts.append("Excelente sinergia de portfólio.")
        elif synergy >= 50:
            parts.append("Sinergia moderada — há espaço para melhorar diversificação.")
        else:
            parts.append("Baixa sinergia — portfólio pode ter estratégias redundantes.")

        if conflicting_pairs:
            parts.append(f"Pares com alta correlação (>0.8): {', '.join(conflicting_pairs[:3])}. Considerar eliminar redundâncias.")

        if hedge_pairs:
            best_hedge = max(hedge_pairs, key=lambda p: p.hedge_compatibility_score)
            parts.append(
                f"Melhor par de hedge: {best_hedge.strategy_a} × {best_hedge.strategy_b} "
                f"(score={best_hedge.hedge_compatibility_score:.0f})."
            )

        return " ".join(parts) if parts else "Análise de sinergia inconclusiva — coletar mais dados."

    def _minimal_report(self, strategy_ids: list[str]) -> MetaStrategyReport:
        return MetaStrategyReport(
            strategy_ids=strategy_ids,
            correlation_matrix={},
            diversification_synergy_score=0.0,
            hedge_pairs=[],
            conflicting_pairs=[],
            best_combination=list(strategy_ids),
            recommendation="Mínimo 2 estratégias necessárias para análise meta.",
            computed_at=datetime.now(timezone.utc).isoformat(),
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meta-Strategy Intelligence — Phase N FASE 5"
    )
    parser.add_argument("--strategies", nargs="+", help="strategy_ids")
    parser.add_argument("--all",  action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    strategy_ids = args.strategies or (
        [f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"]
        if args.all else []
    )

    if len(strategy_ids) < 2:
        print("Forneça pelo menos 2 estratégias com --strategies ou use --all.")
        return

    engine = MetaStrategyIntelligence()
    report = engine.analyze(strategy_ids)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"\nMeta-Strategy Intelligence — {len(report.strategy_ids)} estratégias")
        print(f"  diversification_synergy: {report.diversification_synergy_score:.0f}/100")
        print(f"  hedge_pairs:             {len(report.hedge_pairs)}")
        print(f"  conflicting_pairs:       {len(report.conflicting_pairs)}")
        print(f"  best_combination:        {report.best_combination}")
        if report.conflicting_pairs:
            print(f"  ⚠️ Conflitantes: {', '.join(report.conflicting_pairs)}")
        if report.hedge_pairs:
            print("  Pares de hedge:")
            for p in report.hedge_pairs[:3]:
                print(f"    {p.strategy_a} × {p.strategy_b}: score={p.hedge_compatibility_score:.0f}, corr={p.regime_correlation}")
        print(f"\n  → {report.recommendation}")


if __name__ == "__main__":
    main()

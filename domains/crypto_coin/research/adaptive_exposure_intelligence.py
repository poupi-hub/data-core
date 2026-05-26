"""
adaptive_exposure_intelligence.py — Phase N FASE 4

Adaptive Exposure Intelligence.

Expande a allocation adaptativa (Phase M) com gestão dinâmica de exposição:
  - Reduz exposição em stress, drift e degradação
  - Adapta sizing por robustez, regime e volatilidade
  - Integra sinais de MarketDriftIntelligence e StrategyLifecycleEngine

Scores produzidos:
  - adaptive_exposure_score: exposição recomendada composta (0–100, 100 = plena)
  - stress_exposure_score:   penalidade por stress (0–100, 100 = sem stress)
  - regime_exposure_score:   qualidade de exposição para o regime atual (0–100)

IMPORTANTE:
  - NÃO executa operações reais. Recomendações apenas.
  - Requer confirmação humana antes de qualquer ajuste de exposição.

CLI:
  python -m domains.crypto_coin.research.adaptive_exposure_intelligence --strategies trend_following
  python -m domains.crypto_coin.research.adaptive_exposure_intelligence --all --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.strategy_degradation_intelligence import (
    StrategyDegradationIntelligence,
    DegradationFleetAnalyzer,
)
from domains.crypto_coin.research.fragility_intelligence import FragilityIntelligenceAnalyzer
from domains.crypto_coin.research.regime_aware_intelligence import RegimeAwareIntelligence
from domains.crypto_coin.research.market_drift_intelligence import MarketDriftIntelligence
from domains.crypto_coin.research.strategy_lifecycle import StrategyLifecycleEngine

EXPERIMENTS_DIR = Path("data/experiments")

# Prometheus (optional)
try:
    from api.metrics import adaptive_exposure_score as _prom_exposure
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────────

# Caps de exposição por estado de lifecycle
EXPOSURE_CAP_BY_STATE = {
    "experimental": 0.30,   # máx 30%
    "candidate":    0.60,   # máx 60%
    "validated":    1.00,   # máx 100%
    "degraded":     0.20,   # máx 20%
    "retired":      0.00,   # sem exposição
}

# Penalidade de drift no exposure
DRIFT_CRITICAL_PENALTY  = 0.40   # market_drift >= 70 → reduz 40%
DRIFT_HIGH_PENALTY      = 0.20   # market_drift >= 40 → reduz 20%

# Penalidade de fragility
FRAGILITY_HIGH_PENALTY  = 0.15   # fragility >= 70 → reduz 15%


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class StrategyExposureRecommendation:
    """Recomendação de exposição para uma estratégia específica."""
    strategy_id:             str
    lifecycle_state:         str

    adaptive_exposure_score: float  # 0–100 (100 = plena exposição recomendada)
    stress_exposure_score:   float  # 0–100 (100 = sem penalidade de stress)
    regime_exposure_score:   float  # 0–100 (100 = regime ótimo)

    # Exposição máxima recomendada como fração do portfólio (0.0–1.0)
    max_exposure_fraction:   float
    effective_cap:           float  # cap final após todas as penalidades

    # Drivers da decisão
    health_score:            float
    composite_risk:          float
    fragility_score:         float
    market_drift_score:      float

    signals:                 list[str]
    warning:                 str
    evaluated_at:            str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExposureIntelligenceReport:
    """Relatório de exposição para toda a frota."""
    strategies:              list[StrategyExposureRecommendation]
    market_drift_score:      float
    fleet_health_avg:        float
    total_recommended_exposure: float  # soma de max_exposure_fraction (deve ser ≤ 1.0)
    portfolio_stress_level:  str   # low | medium | high | critical
    recommendation:          str
    computed_at:             str
    warning:                 str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strategies"] = [asdict(s) for s in self.strategies]
        return d


# ── Engine ────────────────────────────────────────────────────────────────────

class AdaptiveExposureIntelligence:
    """
    FASE 4: Calcula exposição adaptativa por estratégia e frota.

    Princípio:
      1. Base: lifecycle_state define o cap máximo de exposição
      2. Penalidade: drift de mercado reduz exposição de toda a frota
      3. Penalidade: fragilidade reduz exposição individual
      4. Bônus: regime compatível com estratégia aumenta confiança
      5. Resultado: max_exposure_fraction por estratégia

    Output requer aprovação humana antes de qualquer mudança de exposure.
    """

    def __init__(
        self,
        experiments_dir: Path = EXPERIMENTS_DIR,
        current_regime:  str | None = None,
    ):
        self.experiments_dir = experiments_dir
        self.current_regime  = current_regime

    def analyze(self, strategy_ids: list[str]) -> ExposureIntelligenceReport:
        """Analisa exposição adaptativa para a lista de estratégias."""

        # Drift global (afeta toda a frota)
        drift_report = MarketDriftIntelligence(self.experiments_dir).analyze()
        market_drift  = drift_report.market_drift_score

        lifecycle_engine = StrategyLifecycleEngine(self.experiments_dir)

        recommendations: list[StrategyExposureRecommendation] = []

        for sid in strategy_ids:
            try:
                rec = self._analyze_single(sid, market_drift, lifecycle_engine)
                recommendations.append(rec)
            except Exception as e:
                recommendations.append(self._error_recommendation(sid, str(e)))

        # Portfolio-level
        fleet_health_avg = statistics.mean(
            r.health_score for r in recommendations if r.health_score > 0
        ) if recommendations else 50.0

        total_exposure = sum(r.max_exposure_fraction for r in recommendations)

        if market_drift >= 70 or fleet_health_avg < 35:
            stress_level = "critical"
        elif market_drift >= 40 or fleet_health_avg < 50:
            stress_level = "high"
        elif market_drift >= 20 or fleet_health_avg < 65:
            stress_level = "medium"
        else:
            stress_level = "low"

        if stress_level == "critical":
            recommendation = (
                "Stress crítico: reduzir exposição total significativamente. "
                "Revisar alocação antes do próximo ciclo."
            )
        elif stress_level == "high":
            recommendation = "Stress elevado: aplicar caps de exposição conservadores em estratégias degradadas."
        elif stress_level == "medium":
            recommendation = "Stress moderado: monitorar estratégias degradadas e ajustar exposure gradualmente."
        else:
            recommendation = "Condições normais: exposure pode seguir plano de alocação adaptativa."

        # Emite métrica agregada
        if _METRICS_AVAILABLE:
            try:
                avg_exposure = statistics.mean(r.adaptive_exposure_score for r in recommendations) if recommendations else 0.0
                _prom_exposure.set(avg_exposure)
            except Exception:
                pass

        return ExposureIntelligenceReport(
            strategies               = recommendations,
            market_drift_score       = round(market_drift, 1),
            fleet_health_avg         = round(fleet_health_avg, 1),
            total_recommended_exposure = round(min(1.0, total_exposure), 3),
            portfolio_stress_level   = stress_level,
            recommendation           = recommendation,
            computed_at              = datetime.now(timezone.utc).isoformat(),
            warning                  = "⚠️ PAPER ONLY — Requer aprovação humana antes de ajustar exposição",
        )

    def _analyze_single(
        self,
        strategy_id:     str,
        market_drift:    float,
        lifecycle_engine: StrategyLifecycleEngine,
    ) -> StrategyExposureRecommendation:
        """Analisa exposição adaptativa para uma estratégia."""
        signals: list[str] = []

        # Lifecycle state → base cap
        lifecycle_status = lifecycle_engine.evaluate(strategy_id)
        state = lifecycle_status.lifecycle_state
        base_cap = EXPOSURE_CAP_BY_STATE.get(state, 0.3)

        health_score  = lifecycle_status.health_score
        composite_risk = lifecycle_status.composite_risk

        # Fragility
        try:
            frag_report   = FragilityIntelligenceAnalyzer(strategy_id, self.experiments_dir).analyze()
            fragility_score = frag_report.fragility_score
        except Exception:
            fragility_score = 0.0

        # Regime compatibility
        regime_exposure_score = 50.0
        try:
            reg_report = RegimeAwareIntelligence(strategy_id, self.experiments_dir).analyze()
            if self.current_regime and reg_report.compatibility_matrix:
                for compat in reg_report.compatibility_matrix:
                    if compat.regime == self.current_regime:
                        regime_exposure_score = 100.0 if compat.compatible else 20.0
                        break
            else:
                # Sem regime definido → usa confiança geral
                regime_exposure_score = reg_report.regime_confidence_score
        except Exception:
            pass

        # Stress exposure score: penalidade por fragility + composite_risk
        stress_exposure_score = max(0.0, 100.0 - composite_risk * 0.5 - fragility_score * 0.3)

        # Penalidade de drift global
        drift_penalty = 0.0
        if market_drift >= 70:
            drift_penalty = DRIFT_CRITICAL_PENALTY
            signals.append(f"Drift crítico ({market_drift:.0f}) — penalidade de exposure aplicada")
        elif market_drift >= 40:
            drift_penalty = DRIFT_HIGH_PENALTY
            signals.append(f"Drift elevado ({market_drift:.0f}) — exposure reduzida")

        # Penalidade de fragilidade
        frag_penalty = 0.0
        if fragility_score >= 70:
            frag_penalty = FRAGILITY_HIGH_PENALTY
            signals.append(f"Fragilidade alta ({fragility_score:.0f}) — exposure reduzida")

        # Cap efetivo
        effective_cap = base_cap * (1.0 - drift_penalty) * (1.0 - frag_penalty)
        effective_cap = max(0.0, min(1.0, effective_cap))

        # Adaptive exposure score (0–100)
        adaptive_exposure_score = round(effective_cap * 100.0, 1)

        # Signals do lifecycle
        if state == "retired":
            signals.append("Estratégia retirada — exposição zero")
        elif state == "degraded":
            signals.append("Estratégia degradada — exposure cap reduzido a 20%")

        return StrategyExposureRecommendation(
            strategy_id              = strategy_id,
            lifecycle_state          = state,
            adaptive_exposure_score  = adaptive_exposure_score,
            stress_exposure_score    = round(stress_exposure_score, 1),
            regime_exposure_score    = round(regime_exposure_score, 1),
            max_exposure_fraction    = round(effective_cap, 3),
            effective_cap            = round(effective_cap, 3),
            health_score             = round(health_score, 1),
            composite_risk           = round(composite_risk, 1),
            fragility_score          = round(fragility_score, 1),
            market_drift_score       = round(market_drift, 1),
            signals                  = signals,
            warning                  = "⚠️ PAPER ONLY",
            evaluated_at             = datetime.now(timezone.utc).isoformat(),
        )

    def _error_recommendation(self, strategy_id: str, error: str) -> StrategyExposureRecommendation:
        return StrategyExposureRecommendation(
            strategy_id=strategy_id, lifecycle_state="experimental",
            adaptive_exposure_score=0.0, stress_exposure_score=0.0,
            regime_exposure_score=0.0, max_exposure_fraction=0.0, effective_cap=0.0,
            health_score=0.0, composite_risk=100.0, fragility_score=100.0,
            market_drift_score=0.0,
            signals=[f"Erro na análise: {error}"],
            warning="⚠️ PAPER ONLY",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Adaptive Exposure Intelligence — Phase N FASE 4"
    )
    parser.add_argument("--strategies", nargs="+", help="strategy_ids")
    parser.add_argument("--all",    action="store_true", help="Todas as estratégias")
    parser.add_argument("--regime", help="Regime atual (ex: bull_market)")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    strategy_ids = args.strategies or (
        [f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"]
        if args.all else []
    )

    if not strategy_ids:
        parser.print_help()
        return

    engine = AdaptiveExposureIntelligence(current_regime=args.regime)
    report = engine.analyze(strategy_ids)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"\n⚠️  {report.warning}")
        print(f"\nAdaptive Exposure Intelligence — {len(report.strategies)} estratégias")
        print(f"  market_drift:     {report.market_drift_score:.0f}/100")
        print(f"  fleet_health:     {report.fleet_health_avg:.0f}/100")
        print(f"  stress_level:     {report.portfolio_stress_level}")
        print(f"  total_exposure:   {report.total_recommended_exposure:.1%}")
        print(f"\n{'Estratégia':<25} {'Estado':<14} {'Exposure':>8} {'Stress':>7} {'Regime':>7} {'Cap':>6}")
        print("-" * 75)
        for s in report.strategies:
            print(
                f"{s.strategy_id:<25} {s.lifecycle_state:<14} "
                f"{s.adaptive_exposure_score:>8.0f} {s.stress_exposure_score:>7.0f} "
                f"{s.regime_exposure_score:>7.0f} {s.effective_cap:>6.1%}"
            )
        print(f"\n  → {report.recommendation}")


if __name__ == "__main__":
    main()

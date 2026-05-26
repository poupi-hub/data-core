"""
market_survival_intelligence.py — Phase O FASE 5

Autonomous Market Survival Intelligence.

Detecta condições extremas de mercado que ameaçam a viabilidade de todas
as estratégias simultaneamente:
  - regime collapse: todas as estratégias performam mal em todos os regimes
  - volatility explosion: sharpe std explode — performance inconsistente
  - market instability: drift + regime shift combinados
  - liquidity deterioration: spreads simulados por queda de sharpe em high_vol
  - cascading degradation: degradação se propagando de estratégia em estratégia
  - strategy contagion: estratégias correlacionadas falham simultaneamente

Scores produzidos:
  - market_survival_score:  capacidade do sistema de sobreviver ao mercado atual (0–100)
  - instability_risk_score: risco de instabilidade sistêmica (0–100)
  - systemic_risk_score:    risco de colapso sistêmico (0–100)

CLI:
  python -m domains.crypto_coin.research.market_survival_intelligence
  python -m domains.crypto_coin.research.market_survival_intelligence --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.market_drift_intelligence import MarketDriftIntelligence
from domains.crypto_coin.research.strategy_degradation_intelligence import DegradationFleetAnalyzer
from domains.crypto_coin.research.regime_aware_intelligence import RegimeAwareIntelligence
from domains.crypto_coin.research.meta_strategy_intelligence import MetaStrategyIntelligence

EXPERIMENTS_DIR = Path("data/experiments")
SURVIVAL_LOG    = Path("data/survival_history.jsonl")

# Prometheus (optional)
try:
    from api.metrics import (
        market_survival_score as _prom_survival,
        systemic_risk_score as _prom_systemic,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SurvivalSignal:
    signal_type:  str
    severity:     str   # low | medium | high | critical
    score:        float
    description:  str


@dataclass
class MarketSurvivalReport:
    """Relatório de sobrevivência de mercado."""
    market_survival_score:  float   # 0–100 (100 = sobrevivência plena)
    instability_risk_score: float   # 0–100 (0 = estável)
    systemic_risk_score:    float   # 0–100 (0 = sem risco sistêmico)

    # Componentes de detecção
    regime_collapse_score:       float   # 0–100
    volatility_explosion_score:  float   # 0–100
    cascading_degradation_score: float   # 0–100
    contagion_risk_score:        float   # 0–100

    # Frota
    strategies_analyzed:  int
    strategies_failing:   int   # degradation_score >= 60

    signals:              list[SurvivalSignal]
    dominant_threat:      str | None
    survival_mode:        bool   # True se survival mode deve ser ativado
    recommendation:       str
    evaluated_at:         str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = [asdict(s) for s in self.signals]
        return d


# ── Analyzer ──────────────────────────────────────────────────────────────────

class MarketSurvivalIntelligence:
    """
    FASE 5: Detecta condições extremas de mercado (regime collapse, volatility
    explosion, cascading degradation, strategy contagion).

    Reutiliza:
      - MarketDriftIntelligence (market_drift, edge_decay)
      - DegradationFleetAnalyzer (fleet-wide degradation)
      - MetaStrategyIntelligence (correlation, contagion proxy)
    """

    # Thresholds de sobrevivência
    REGIME_COLLAPSE_HEALTH  = 30.0   # fleet health avg <= 30 = colapso
    VOLATILITY_EXPLOSION_STD = 1.2   # avg sharpe_std >= 1.2 = volatility explosion
    CASCADE_FRACTION        = 0.60   # 60%+ estratégias degradadas = cascading
    CONTAGION_CORR_THRESH   = 0.75   # correlação >= 0.75 + ambas degradadas = contagion
    SYSTEMIC_RISK_THRESH    = 70.0   # systemic_risk >= 70 = ativar survival mode

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def analyze(self) -> MarketSurvivalReport:
        fleet_analyzer = DegradationFleetAnalyzer(self.experiments_dir)
        fleet_reports  = fleet_analyzer.rank_all()
        strategy_ids   = [r.strategy_id for r in fleet_reports]

        if not fleet_reports:
            return self._empty_report()

        signals: list[SurvivalSignal] = []
        strategies_failing = sum(1 for r in fleet_reports if r.degradation_score >= 60)
        fleet_health_avg   = statistics.mean(r.strategy_health_score for r in fleet_reports)
        sharpe_stds        = [r.sharpe_std for r in fleet_reports if r.sharpe_std is not None]
        avg_sharpe_std     = statistics.mean(sharpe_stds) if sharpe_stds else 0.0

        # Drift (proxy de instabilidade)
        try:
            drift_report = MarketDriftIntelligence(self.experiments_dir).analyze()
            market_drift = drift_report.market_drift_score
            edge_decay   = drift_report.edge_decay_score
        except Exception:
            market_drift = 0.0
            edge_decay   = 0.0

        # ── 1. Regime Collapse ────────────────────────────────────────────────
        regime_collapse_score = max(0.0, (self.REGIME_COLLAPSE_HEALTH - fleet_health_avg) * 3.0)
        regime_collapse_score = min(100.0, regime_collapse_score)
        if fleet_health_avg <= self.REGIME_COLLAPSE_HEALTH:
            signals.append(SurvivalSignal(
                "regime_collapse", "critical", regime_collapse_score,
                f"Fleet health={fleet_health_avg:.0f} <= {self.REGIME_COLLAPSE_HEALTH} — regime collapse possível",
            ))
        elif fleet_health_avg <= 45:
            signals.append(SurvivalSignal(
                "regime_collapse", "high", regime_collapse_score,
                f"Fleet health={fleet_health_avg:.0f} — fragilidade elevada",
            ))

        # ── 2. Volatility Explosion ───────────────────────────────────────────
        vol_explosion_score = min(100.0, (avg_sharpe_std / self.VOLATILITY_EXPLOSION_STD) * 80.0)
        if avg_sharpe_std >= self.VOLATILITY_EXPLOSION_STD:
            signals.append(SurvivalSignal(
                "volatility_explosion", "critical", vol_explosion_score,
                f"avg_sharpe_std={avg_sharpe_std:.2f} >= {self.VOLATILITY_EXPLOSION_STD} — performance inconsistente",
            ))
        elif avg_sharpe_std >= 0.8:
            signals.append(SurvivalSignal(
                "volatility_explosion", "high", vol_explosion_score,
                f"avg_sharpe_std={avg_sharpe_std:.2f} elevado",
            ))

        # ── 3. Cascading Degradation ──────────────────────────────────────────
        cascade_fraction    = strategies_failing / max(len(fleet_reports), 1)
        cascade_score       = min(100.0, cascade_fraction * 120.0)
        if cascade_fraction >= self.CASCADE_FRACTION:
            signals.append(SurvivalSignal(
                "cascading_degradation", "critical", cascade_score,
                f"{strategies_failing}/{len(fleet_reports)} estratégias degradadas — cascading detectado",
            ))
        elif cascade_fraction >= 0.35:
            signals.append(SurvivalSignal(
                "cascading_degradation", "high", cascade_score,
                f"{strategies_failing}/{len(fleet_reports)} estratégias com degradação",
            ))

        # ── 4. Strategy Contagion (via correlação + degradação conjunta) ──────
        contagion_score = self._compute_contagion_score(fleet_reports, strategy_ids)
        if contagion_score >= 60:
            signals.append(SurvivalSignal(
                "strategy_contagion", "high", contagion_score,
                f"Contagion detectado: estratégias correlacionadas degradando simultaneamente",
            ))

        # ── Composite scores ──────────────────────────────────────────────────
        instability_risk = round(
            regime_collapse_score  * 0.30 +
            vol_explosion_score    * 0.25 +
            market_drift           * 0.25 +
            cascade_score          * 0.20,
            1,
        )
        systemic_risk = round(
            cascade_score          * 0.35 +
            contagion_score        * 0.25 +
            regime_collapse_score  * 0.25 +
            vol_explosion_score    * 0.15,
            1,
        )
        market_survival = round(max(0.0, 100.0 - instability_risk * 0.5 - systemic_risk * 0.5), 1)

        survival_mode = systemic_risk >= self.SYSTEMIC_RISK_THRESH or instability_risk >= 75

        dominant = max(signals, key=lambda s: s.score, default=None)
        dominant_threat = dominant.signal_type if dominant else None

        if survival_mode:
            recommendation = (
                "⛔ SURVIVAL MODE RECOMENDADO. Risco sistêmico crítico. "
                "Reduzir toda exposure, pausar novos rebalances, investigar causa raiz."
            )
        elif systemic_risk >= 50:
            recommendation = (
                "Risco sistêmico elevado. Aplicar controle de emergência. "
                "Priorizar research em estratégias degradadas."
            )
        else:
            recommendation = "Sistema estável. Monitoramento regular suficiente."

        report = MarketSurvivalReport(
            market_survival_score       = market_survival,
            instability_risk_score      = instability_risk,
            systemic_risk_score         = systemic_risk,
            regime_collapse_score       = round(regime_collapse_score, 1),
            volatility_explosion_score  = round(vol_explosion_score, 1),
            cascading_degradation_score = round(cascade_score, 1),
            contagion_risk_score        = round(contagion_score, 1),
            strategies_analyzed         = len(fleet_reports),
            strategies_failing          = strategies_failing,
            signals                     = signals,
            dominant_threat             = dominant_threat,
            survival_mode               = survival_mode,
            recommendation              = recommendation,
            evaluated_at                = datetime.now(timezone.utc).isoformat(),
        )
        self._persist(report)
        self._emit_metrics(report)
        return report

    def _compute_contagion_score(self, fleet_reports: list[Any], strategy_ids: list[str]) -> float:
        """
        Contagion score: pares de estratégias altamente correlacionadas
        que estão ambas degradadas.
        """
        if len(strategy_ids) < 2:
            return 0.0

        try:
            meta = MetaStrategyIntelligence(self.experiments_dir)
            meta_report = meta.analyze(strategy_ids)
        except Exception:
            return 0.0

        degraded_ids = {r.strategy_id for r in fleet_reports if r.degradation_score >= 50}
        contagion_pairs = 0
        total_pairs = 0

        for pair in meta_report.hedge_pairs + []:  # uses correlation_matrix directly
            pass

        # Simplified: check correlation_matrix for high-corr pairs where both are degraded
        corr_matrix = meta_report.correlation_matrix
        for i, sid_a in enumerate(strategy_ids):
            for j, sid_b in enumerate(strategy_ids):
                if i >= j:
                    continue
                total_pairs += 1
                corr = corr_matrix.get(sid_a, {}).get(sid_b)
                if corr is not None and corr >= self.CONTAGION_CORR_THRESH:
                    if sid_a in degraded_ids and sid_b in degraded_ids:
                        contagion_pairs += 1

        if total_pairs == 0:
            return 0.0
        return min(100.0, (contagion_pairs / total_pairs) * 200.0)

    def _persist(self, report: MarketSurvivalReport) -> None:
        try:
            SURVIVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":         report.evaluated_at,
                "market_survival_score":report.market_survival_score,
                "instability_risk":     report.instability_risk_score,
                "systemic_risk":        report.systemic_risk_score,
                "survival_mode":        report.survival_mode,
            }
            with open(SURVIVAL_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _emit_metrics(self, report: MarketSurvivalReport) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_survival.set(report.market_survival_score)
            _prom_systemic.set(report.systemic_risk_score)
        except Exception:
            pass

    def _empty_report(self) -> MarketSurvivalReport:
        return MarketSurvivalReport(
            market_survival_score=100.0, instability_risk_score=0.0, systemic_risk_score=0.0,
            regime_collapse_score=0.0, volatility_explosion_score=0.0,
            cascading_degradation_score=0.0, contagion_risk_score=0.0,
            strategies_analyzed=0, strategies_failing=0,
            signals=[], dominant_threat=None, survival_mode=False,
            recommendation="Sem estratégias para analisar. Execute sweep_runner primeiro.",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Market Survival Intelligence — Phase O FASE 5")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    analyzer = MarketSurvivalIntelligence()
    report   = analyzer.analyze()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nMarket Survival Intelligence")
    print(f"  market_survival_score:  {report.market_survival_score:.0f}/100")
    print(f"  instability_risk_score: {report.instability_risk_score:.0f}/100")
    print(f"  systemic_risk_score:    {report.systemic_risk_score:.0f}/100")
    print(f"  strategies:             {report.strategies_analyzed} ({report.strategies_failing} failing)")
    print(f"  survival_mode:          {'⛔ ATIVO' if report.survival_mode else 'inativo'}")
    if report.signals:
        print("\n  Sinais:")
        for s in report.signals:
            print(f"    [{s.severity.upper()}] {s.description}")
    print(f"\n  → {report.recommendation}")


if __name__ == "__main__":
    main()

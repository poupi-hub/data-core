"""
adaptive_risk_intelligence.py — Phase O FASE 9

Adaptive Risk Intelligence.

Detecta riscos emergentes e ocultos que os modulos anteriores nao capturam:
  - cascading_losses:    perdas em cadeia por correlacao nao detectada
  - correlated_failures: falhas sincronizadas em estrategias aparentemente independentes
  - parameter_explosion: parametros otimizados que explodem fora do range de treino
  - hidden_fragility:    estrategias que parecem saudaveis mas sao frageis a stress
  - tail_risk:           risco de cauda assimetrico (downside muito maior que upside)

Scores produzidos:
  - adaptive_risk_score:    risco adaptativo total (0-100, 0=seguro)
  - contagion_risk_score:   risco de contagio entre estrategias (0-100)
  - hidden_fragility_score: fragilidade oculta detectada (0-100)

CLI:
  python -m domains.crypto_coin.research.adaptive_risk_intelligence
  python -m domains.crypto_coin.research.adaptive_risk_intelligence --json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.strategy_degradation_intelligence import DegradationFleetAnalyzer
from domains.crypto_coin.research.meta_strategy_intelligence import MetaStrategyIntelligence
from domains.crypto_coin.research.parameter_intelligence import ParameterIntelligenceFleet
from domains.crypto_coin.research.market_drift_intelligence import MarketDriftIntelligence
from domains.crypto_coin.research.experiment_tracker import ExperimentTracker

EXPERIMENTS_DIR = Path("data/experiments")
RISK_LOG        = Path("data/adaptive_risk_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import adaptive_risk_score as _prom_risk
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

# Contagion
CONTAGION_CORR_THRESH     = 0.70   # correlacao >= 0.70 para par de risco
CONTAGION_DEGRAD_THRESH   = 55.0   # degradacao >= 55 para considerar "em risco"

# Hidden fragility
FRAGILITY_SHARPE_STD      = 0.80   # sharpe_std >= 0.80 = alta variabilidade
FRAGILITY_DRAWDOWN_THRESH = -0.30  # max_drawdown <= -30% em algum regime = sinal
FRAGILITY_HEALTH_HIGH     = 65.0   # health >= 65 mas fragil = hidden

# Tail risk
TAIL_RATIO_THRESH         = 2.5    # ratio downside/upside >= 2.5 = tail risk

# Parameter explosion
PARAM_EXPLOSION_RATIO     = 5.0    # pico / mediana >= 5.0 = parametro explosivo


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class RiskSignal:
    signal_type:  str    # cascading_loss | correlated_failure | parameter_explosion | hidden_fragility | tail_risk
    severity:     str    # low | medium | high | critical
    score:        float  # 0-100
    strategy_id:  str | None
    description:  str


@dataclass
class AdaptiveRiskReport:
    """Relatorio de risco adaptativo e oculto."""
    adaptive_risk_score:    float   # 0-100 (0 = seguro)
    contagion_risk_score:   float   # 0-100
    hidden_fragility_score: float   # 0-100

    # Componentes
    cascading_loss_score:      float   # 0-100
    correlated_failure_score:  float   # 0-100
    parameter_explosion_score: float   # 0-100
    tail_risk_score:           float   # 0-100

    # Frota
    strategies_analyzed:    int
    strategies_at_risk:     int   # adaptive_risk >= 60
    contagion_pairs:        int   # pares com corr alta + ambos degradados

    signals:                list[RiskSignal]
    dominant_risk_type:     str | None
    risk_recommendation:    str
    evaluated_at:           str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = [asdict(s) for s in self.signals]
        return d


# ── Analyzer ───────────────────────────────────────────────────────────────────

class AdaptiveRiskIntelligence:
    """
    FASE 9: Detecta riscos emergentes e ocultos na frota de estrategias.

    Reutiliza:
      - DegradationFleetAnalyzer (degradation, health, sharpe_std, fragility)
      - MetaStrategyIntelligence (correlation_matrix)
      - ParameterIntelligenceFleet (peak_isolation, is_fragile)
      - MarketDriftIntelligence (market_drift)
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def analyze(self) -> AdaptiveRiskReport:
        strategy_files = list(self.experiments_dir.glob("*.jsonl"))
        strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]

        if not strategy_ids:
            return self._empty_report()

        signals: list[RiskSignal] = []

        fleet_analyzer = DegradationFleetAnalyzer(self.experiments_dir)
        fleet_reports  = fleet_analyzer.rank_all()
        degrad_map     = {r.strategy_id: r for r in fleet_reports}

        # Market drift
        try:
            drift_report = MarketDriftIntelligence(self.experiments_dir).analyze()
            market_drift = drift_report.market_drift_score
        except Exception:
            market_drift = 0.0

        # ── 1. Contagion Risk (correlacao + degradacao conjunta) ───────────────
        contagion_score, contagion_pairs, contagion_signals = self._detect_contagion(
            strategy_ids, degrad_map
        )
        signals.extend(contagion_signals)

        # ── 2. Parameter Explosion ────────────────────────────────────────────
        param_score, param_signals = self._detect_parameter_explosion(strategy_ids)
        signals.extend(param_signals)

        # ── 3. Hidden Fragility ───────────────────────────────────────────────
        hidden_frag_score, hidden_signals = self._detect_hidden_fragility(
            strategy_ids, degrad_map, fleet_reports
        )
        signals.extend(hidden_signals)

        # ── 4. Tail Risk ──────────────────────────────────────────────────────
        tail_score, tail_signals = self._detect_tail_risk(strategy_ids)
        signals.extend(tail_signals)

        # ── 5. Cascading Loss (proxy via fleet degradation momentum) ──────────
        cascade_score = self._compute_cascading_loss_score(fleet_reports, market_drift)
        if cascade_score >= 40:
            signals.append(RiskSignal(
                "cascading_loss", "high" if cascade_score >= 70 else "medium",
                cascade_score, None,
                f"Risco de cascata detectado: fleet_degrad elevado com drift={market_drift:.0f}",
            ))

        # ── Composite ─────────────────────────────────────────────────────────
        correlated_failure_score = min(100.0, contagion_score * 1.1)

        adaptive_risk = round(
            cascade_score          * 0.25 +
            contagion_score        * 0.25 +
            param_score            * 0.20 +
            hidden_frag_score      * 0.20 +
            tail_score             * 0.10,
            1,
        )

        strategies_at_risk = sum(
            1 for r in fleet_reports
            if r.degradation_score >= CONTAGION_DEGRAD_THRESH
        )

        dominant = max(signals, key=lambda s: s.score, default=None)
        dominant_type = dominant.signal_type if dominant else None

        recommendation = self._build_recommendation(
            adaptive_risk, contagion_score, hidden_frag_score, contagion_pairs
        )

        report = AdaptiveRiskReport(
            adaptive_risk_score         = adaptive_risk,
            contagion_risk_score        = round(contagion_score, 1),
            hidden_fragility_score      = round(hidden_frag_score, 1),
            cascading_loss_score        = round(cascade_score, 1),
            correlated_failure_score    = round(correlated_failure_score, 1),
            parameter_explosion_score   = round(param_score, 1),
            tail_risk_score             = round(tail_score, 1),
            strategies_analyzed         = len(strategy_ids),
            strategies_at_risk          = strategies_at_risk,
            contagion_pairs             = contagion_pairs,
            signals                     = signals,
            dominant_risk_type          = dominant_type,
            risk_recommendation         = recommendation,
            evaluated_at                = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_risk.set(adaptive_risk)
            except Exception:
                pass

        return report

    # ── Detection methods ──────────────────────────────────────────────────────

    def _detect_contagion(
        self,
        strategy_ids: list[str],
        degrad_map: dict,
    ) -> tuple[float, int, list[RiskSignal]]:
        signals: list[RiskSignal] = []
        contagion_pairs = 0
        total_pairs = 0

        if len(strategy_ids) < 2:
            return 0.0, 0, signals

        try:
            meta = MetaStrategyIntelligence(self.experiments_dir)
            meta_report = meta.analyze(strategy_ids)
            corr_matrix = meta_report.correlation_matrix
        except Exception:
            return 0.0, 0, signals

        degraded_ids = {
            sid for sid, r in degrad_map.items()
            if r.degradation_score >= CONTAGION_DEGRAD_THRESH
        }

        for i, sid_a in enumerate(strategy_ids):
            for j, sid_b in enumerate(strategy_ids):
                if i >= j:
                    continue
                total_pairs += 1
                corr = corr_matrix.get(sid_a, {}).get(sid_b)
                if corr is None:
                    continue
                if corr >= CONTAGION_CORR_THRESH:
                    if sid_a in degraded_ids and sid_b in degraded_ids:
                        contagion_pairs += 1
                        signals.append(RiskSignal(
                            "correlated_failure", "high", min(100.0, corr * 100),
                            f"{sid_a}+{sid_b}",
                            f"Par correlacionado ({corr:.2f}) com ambas estrategias degradadas",
                        ))

        if total_pairs == 0:
            return 0.0, contagion_pairs, signals

        contagion_score = min(100.0, (contagion_pairs / total_pairs) * 300.0)
        return contagion_score, contagion_pairs, signals

    def _detect_parameter_explosion(
        self, strategy_ids: list[str]
    ) -> tuple[float, list[RiskSignal]]:
        signals: list[RiskSignal] = []
        explosion_scores: list[float] = []

        try:
            fleet = ParameterIntelligenceFleet(self.experiments_dir)
            param_reports = fleet.analyze_all()
        except Exception:
            return 0.0, signals

        for rep in param_reports:
            if rep.strategy_id not in strategy_ids:
                continue
            if rep.is_fragile and rep.peak_isolation:
                score = min(100.0, rep.peak_isolation * 20.0)
                explosion_scores.append(score)
                if score >= 50:
                    signals.append(RiskSignal(
                        "parameter_explosion",
                        "high" if score >= 75 else "medium",
                        score,
                        rep.strategy_id,
                        f"Parametro explosivo: peak_isolation={rep.peak_isolation:.2f}, stability={rep.parameter_stability_score:.0f}",
                    ))

        avg_score = statistics.mean(explosion_scores) if explosion_scores else 0.0
        return avg_score, signals

    def _detect_hidden_fragility(
        self,
        strategy_ids: list[str],
        degrad_map:   dict,
        fleet_reports: list,
    ) -> tuple[float, list[RiskSignal]]:
        """Estrategias com health alto mas sharpe_std alto ou drawdown severo em algum regime."""
        signals: list[RiskSignal] = []
        frag_scores: list[float] = []

        for rep in fleet_reports:
            if rep.strategy_id not in strategy_ids:
                continue

            health = rep.strategy_health_score
            sharpe_std = rep.sharpe_std or 0.0

            # Hidden: health aparentemente ok mas alta variabilidade
            if health >= FRAGILITY_HEALTH_HIGH and sharpe_std >= FRAGILITY_SHARPE_STD:
                score = min(100.0, sharpe_std * 60.0 + (health - 65.0) * 0.5)
                frag_scores.append(score)
                signals.append(RiskSignal(
                    "hidden_fragility", "high" if score >= 70 else "medium",
                    score, rep.strategy_id,
                    f"Saudavel (health={health:.0f}) mas sharpe_std={sharpe_std:.2f} — fragilidade oculta",
                ))

            # Drawdown severo em algum regime
            try:
                tracker = ExperimentTracker(rep.strategy_id, self.experiments_dir)
                experiments = tracker.load_experiments()
                drawdowns = [e.metrics.get("max_drawdown", 0.0) for e in experiments]
                worst_dd = min(drawdowns) if drawdowns else 0.0
                if worst_dd <= FRAGILITY_DRAWDOWN_THRESH and health >= 50:
                    score = min(100.0, abs(worst_dd) * 150.0)
                    frag_scores.append(score)
                    signals.append(RiskSignal(
                        "hidden_fragility", "medium",
                        score, rep.strategy_id,
                        f"Drawdown severo ({worst_dd:.1%}) com health={health:.0f} — tail risk latente",
                    ))
            except Exception:
                pass

        avg_score = statistics.mean(frag_scores) if frag_scores else 0.0
        return avg_score, signals

    def _detect_tail_risk(
        self, strategy_ids: list[str]
    ) -> tuple[float, list[RiskSignal]]:
        """Detecta assimetria downside/upside por estrategia."""
        signals: list[RiskSignal] = []
        tail_scores: list[float] = []

        for sid in strategy_ids:
            try:
                tracker = ExperimentTracker(sid, self.experiments_dir)
                experiments = tracker.load_experiments()
                if len(experiments) < 3:
                    continue

                sharpes = [e.metrics.get("sharpe", 0.0) for e in experiments]
                positive = [s for s in sharpes if s > 0]
                negative = [s for s in sharpes if s < 0]

                if not positive or not negative:
                    continue

                avg_pos = statistics.mean(positive)
                avg_neg = abs(statistics.mean(negative))

                if avg_pos > 0:
                    tail_ratio = avg_neg / avg_pos
                    if tail_ratio >= TAIL_RATIO_THRESH:
                        score = min(100.0, tail_ratio * 20.0)
                        tail_scores.append(score)
                        signals.append(RiskSignal(
                            "tail_risk", "high" if score >= 70 else "medium",
                            score, sid,
                            f"Tail ratio={tail_ratio:.2f} (downside/upside) — assimetria critica",
                        ))
            except Exception:
                continue

        avg_score = statistics.mean(tail_scores) if tail_scores else 0.0
        return avg_score, signals

    def _compute_cascading_loss_score(self, fleet_reports: list, market_drift: float) -> float:
        if not fleet_reports:
            return 0.0
        avg_degrad = statistics.mean(r.degradation_score for r in fleet_reports)
        drift_amplifier = 1.0 + max(0.0, (market_drift - 50.0) / 100.0)
        return min(100.0, avg_degrad * drift_amplifier * 0.8)

    def _build_recommendation(
        self,
        adaptive_risk: float,
        contagion:     float,
        hidden_frag:   float,
        contagion_pairs: int,
    ) -> str:
        if adaptive_risk >= 75:
            return "RISCO ADAPTATIVO CRITICO. Reducao de exposure imediata. Investigar contagio e fragilidade oculta."
        if contagion >= 60 and contagion_pairs > 0:
            return f"{contagion_pairs} par(es) de estrategias em contagio. Desativar uma de cada par correlacionado."
        if hidden_frag >= 60:
            return "Fragilidade oculta detectada. Estrategias aparecem saudaveis mas sao vulneraveis a stress."
        if adaptive_risk >= 40:
            return "Risco adaptativo moderado. Monitorar sharpe_std e drawdowns. Reducao preventiva de sizing."
        return "Risco adaptativo baixo. Sistema dentro dos parametros normais."

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: AdaptiveRiskReport) -> None:
        try:
            RISK_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":          report.evaluated_at,
                "adaptive_risk_score":   report.adaptive_risk_score,
                "contagion_risk_score":  report.contagion_risk_score,
                "hidden_fragility_score": report.hidden_fragility_score,
                "contagion_pairs":       report.contagion_pairs,
                "strategies_at_risk":    report.strategies_at_risk,
            }
            with open(RISK_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _empty_report(self) -> AdaptiveRiskReport:
        return AdaptiveRiskReport(
            adaptive_risk_score=0.0, contagion_risk_score=0.0,
            hidden_fragility_score=0.0, cascading_loss_score=0.0,
            correlated_failure_score=0.0, parameter_explosion_score=0.0,
            tail_risk_score=0.0, strategies_analyzed=0, strategies_at_risk=0,
            contagion_pairs=0, signals=[], dominant_risk_type=None,
            risk_recommendation="Sem estrategias para analisar.",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Adaptive Risk Intelligence — Phase O FASE 9")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    analyzer = AdaptiveRiskIntelligence()
    report   = analyzer.analyze()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nAdaptive Risk Intelligence")
    print(f"  adaptive_risk_score:    {report.adaptive_risk_score:.0f}/100")
    print(f"  contagion_risk_score:   {report.contagion_risk_score:.0f}/100")
    print(f"  hidden_fragility_score: {report.hidden_fragility_score:.0f}/100")
    print(f"  tail_risk_score:        {report.tail_risk_score:.0f}/100")
    print(f"  parameter_explosion:    {report.parameter_explosion_score:.0f}/100")
    print(f"  contagion_pairs:        {report.contagion_pairs}")
    print(f"  strategies_at_risk:     {report.strategies_at_risk}/{report.strategies_analyzed}")
    if report.dominant_risk_type:
        print(f"  dominant_risk:          {report.dominant_risk_type}")
    if report.signals:
        print("\n  Sinais:")
        for s in report.signals[:10]:
            sid_str = f" [{s.strategy_id}]" if s.strategy_id else ""
            print(f"    [{s.severity.upper()}] {s.signal_type}{sid_str}: {s.description}")
    print(f"\n  -> {report.risk_recommendation}")


if __name__ == "__main__":
    main()

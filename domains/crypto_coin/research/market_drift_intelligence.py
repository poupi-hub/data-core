"""
market_drift_intelligence.py — Phase N FASE 2

Continuous Market Drift Intelligence.

Detecta mudanças estruturais no mercado utilizando dados de experimentos históricos
como proxy de performance de edge (sem dependência de live market feed).

Scores produzidos:
  - market_drift_score:     índice composto de drift 0–100
  - regime_shift_score:     quão recentemente as estratégias mudaram de regime ótimo
  - edge_decay_score:       decaimento do edge quantitativo da frota
  - volatility_shift_score: mudança estrutural de volatilidade inferida do sharpe

Princípio anti-duplicação:
  Reutiliza StrategyDegradationIntelligence, DegradationFleetAnalyzer e
  RegimeAwareIntelligence. NÃO reimplementa replay ou backtesting.

Persistência:
  Salva histórico de drift em data/drift_history.jsonl para rastreabilidade.

CLI:
  python -m domains.crypto_coin.research.market_drift_intelligence
  python -m domains.crypto_coin.research.market_drift_intelligence --json
  python -m domains.crypto_coin.research.market_drift_intelligence --days 7
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
from domains.crypto_coin.research.regime_aware_intelligence import RegimeAwareIntelligence

EXPERIMENTS_DIR = Path("data/experiments")
DRIFT_HISTORY_FILE = Path("data/drift_history.jsonl")

# Prometheus metrics (optional)
try:
    from api.metrics import (
        market_drift_score as _prom_drift,
        edge_decay_score as _prom_edge_decay,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DriftSignal:
    """Sinal individual de drift detectado."""
    signal_type:  str   # edge_decay | regime_shift | volatility_shift | fleet_degradation
    severity:     str   # low | medium | high | critical
    score:        float
    description:  str


@dataclass
class MarketDriftReport:
    """
    Relatório completo de drift de mercado baseado em análise de frota.

    Todos os scores são 0–100:
      0  = sem drift / edge intacto
      100 = drift severo / edge totalmente deteriorado
    """
    market_drift_score:    float   # score composto 0–100
    regime_shift_score:    float   # mudança de regime ótimo entre estratégias
    edge_decay_score:      float   # decaimento de edge (sharpe médio frota)
    volatility_shift_score: float  # inferida de variância crescente do sharpe

    # Frota
    strategies_analyzed:   int
    strategies_degraded:   int   # composite_risk >= 50
    fleet_health_avg:      float

    # Sinais textuais
    signals:               list[DriftSignal]
    dominant_signal:       str | None
    recommendation:        str

    # Lineage
    evaluated_at:          str
    experiments_total:     int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = [asdict(s) for s in self.signals]
        return d


# ── Analyzer ──────────────────────────────────────────────────────────────────

class MarketDriftIntelligence:
    """
    Detecta drift estrutural de mercado via análise de frota de estratégias.

    Método:
      1. Edge decay:       declínio do sharpe médio da frota no tempo
      2. Regime shift:     mudança de regime ótimo entre estratégias
      3. Volatility shift: aumento do desvio padrão do sharpe (instabilidade)
      4. Fleet degradation: fração da frota com composite_risk >= 50
    """

    # Thresholds
    EDGE_DECAY_CRITICAL   = 70.0  # score de degradação médio crítico
    EDGE_DECAY_HIGH       = 45.0
    REGIME_SHIFT_THRESHOLD = 0.5  # 50%+ estratégias mudaram regime ótimo
    VOLATILITY_STD_HIGH   = 0.7   # std do sharpe aumentou mais de 0.7

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def analyze(self) -> MarketDriftReport:
        """Executa análise completa de drift de mercado."""
        fleet_analyzer = DegradationFleetAnalyzer(self.experiments_dir)
        fleet_reports  = fleet_analyzer.rank_all()

        if not fleet_reports:
            return self._empty_report("Nenhuma estratégia analisada")

        strategies_analyzed  = len(fleet_reports)
        strategies_degraded  = sum(1 for r in fleet_reports if r.composite_risk_score >= 50)
        fleet_health_avg     = statistics.mean(r.strategy_health_score for r in fleet_reports)
        experiments_total    = sum(r.experiments_analyzed for r in fleet_reports)

        signals: list[DriftSignal] = []

        # ── 1. Edge Decay Score ───────────────────────────────────────────────
        edge_decay_score = self._compute_edge_decay(fleet_reports)
        if edge_decay_score >= 70:
            signals.append(DriftSignal(
                signal_type="edge_decay", severity="critical", score=edge_decay_score,
                description=f"Edge decaindo criticamente — degradação média da frota: {edge_decay_score:.0f}/100",
            ))
        elif edge_decay_score >= 40:
            signals.append(DriftSignal(
                signal_type="edge_decay", severity="high", score=edge_decay_score,
                description=f"Edge decaindo — degradação média moderada: {edge_decay_score:.0f}/100",
            ))

        # ── 2. Regime Shift Score ─────────────────────────────────────────────
        regime_shift_score = self._compute_regime_shift(fleet_reports)
        if regime_shift_score >= 60:
            signals.append(DriftSignal(
                signal_type="regime_shift", severity="high", score=regime_shift_score,
                description=f"Mudança de regime detectada — {regime_shift_score:.0f}% das estratégias em regime diferente",
            ))
        elif regime_shift_score >= 35:
            signals.append(DriftSignal(
                signal_type="regime_shift", severity="medium", score=regime_shift_score,
                description=f"Possível mudança de regime ({regime_shift_score:.0f}%)",
            ))

        # ── 3. Volatility Shift Score ─────────────────────────────────────────
        volatility_shift_score = self._compute_volatility_shift(fleet_reports)
        if volatility_shift_score >= 65:
            signals.append(DriftSignal(
                signal_type="volatility_shift", severity="high", score=volatility_shift_score,
                description=f"Volatilidade estrutural elevada — sharpe instável (score={volatility_shift_score:.0f})",
            ))

        # ── 4. Fleet Degradation Signal ───────────────────────────────────────
        fleet_degrad_pct = (strategies_degraded / strategies_analyzed) * 100
        if fleet_degrad_pct >= 60:
            signals.append(DriftSignal(
                signal_type="fleet_degradation", severity="critical", score=fleet_degrad_pct,
                description=f"{strategies_degraded}/{strategies_analyzed} estratégias degradadas",
            ))
        elif fleet_degrad_pct >= 33:
            signals.append(DriftSignal(
                signal_type="fleet_degradation", severity="medium", score=fleet_degrad_pct,
                description=f"{strategies_degraded}/{strategies_analyzed} estratégias em risco",
            ))

        # ── Composite Market Drift Score ──────────────────────────────────────
        market_drift_score = round(
            edge_decay_score     * 0.40 +
            volatility_shift_score * 0.30 +
            regime_shift_score   * 0.20 +
            fleet_degrad_pct     * 0.10,
            1,
        )
        market_drift_score = max(0.0, min(100.0, market_drift_score))

        # ── Dominant signal ───────────────────────────────────────────────────
        dominant = max(signals, key=lambda s: s.score, default=None)
        dominant_signal = dominant.signal_type if dominant else None

        # ── Recommendation ────────────────────────────────────────────────────
        if market_drift_score >= 70:
            recommendation = (
                "Drift crítico detectado. Pausar alocação adaptativa, executar sweep completo "
                "e revisar regime assumptions."
            )
        elif market_drift_score >= 40:
            recommendation = (
                "Drift moderado. Reduzir exposure em estratégias degradadas, "
                "priorizar replay em out-of-sample recente."
            )
        else:
            recommendation = (
                "Sem drift significativo detectado. Continuar monitoramento regular."
            )

        report = MarketDriftReport(
            market_drift_score    = market_drift_score,
            regime_shift_score    = round(regime_shift_score, 1),
            edge_decay_score      = round(edge_decay_score, 1),
            volatility_shift_score= round(volatility_shift_score, 1),
            strategies_analyzed   = strategies_analyzed,
            strategies_degraded   = strategies_degraded,
            fleet_health_avg      = round(fleet_health_avg, 1),
            signals               = signals,
            dominant_signal       = dominant_signal,
            recommendation        = recommendation,
            evaluated_at          = datetime.now(timezone.utc).isoformat(),
            experiments_total     = experiments_total,
        )

        self._persist(report)
        self._emit_metrics(report)
        return report

    # ── Score computations ────────────────────────────────────────────────────

    def _compute_edge_decay(self, fleet_reports: list[Any]) -> float:
        """
        Edge decay = média do degradation_score da frota.
        High degradation_score → high edge decay.
        """
        degradations = [r.degradation_score for r in fleet_reports]
        if not degradations:
            return 0.0
        return round(statistics.mean(degradations), 1)

    def _compute_regime_shift(self, fleet_reports: list[Any]) -> float:
        """
        Regime shift = % de estratégias cujo best_regime está definido
        multiplicado pela dispersão de regimes (muita dispersão = shift em curso).

        Proxy: se a frota está convergindo para regimes diferentes → possível transição.
        """
        best_regimes: list[str] = []
        for r in fleet_reports:
            try:
                reg = RegimeAwareIntelligence(r.strategy_id, self.experiments_dir).analyze()
                if reg.best_regime:
                    best_regimes.append(reg.best_regime)
            except Exception:
                pass

        if len(best_regimes) < 2:
            return 0.0

        # Diversidade de regime: se todas estão no mesmo → 0 shift
        # Se cada estratégia está em regime diferente → possível shift
        from collections import Counter
        counts = Counter(best_regimes)
        majority = max(counts.values())
        majority_pct = majority / len(best_regimes)
        # Pouca maioria → alta dispersão → regime shift possível
        regime_shift = round((1.0 - majority_pct) * 100, 1)
        return regime_shift

    def _compute_volatility_shift(self, fleet_reports: list[Any]) -> float:
        """
        Volatility shift = instabilidade de sharpe da frota.
        Usa sharpe_std de cada estratégia. std alto → volatilidade crescente.
        """
        stds = [r.sharpe_std for r in fleet_reports if r.sharpe_std is not None]
        if not stds:
            return 0.0
        avg_std = statistics.mean(stds)
        # avg_std < 0.2 = estável (0), avg_std > 1.0 = volátil (100)
        score = min(100.0, (avg_std / 0.7) * 65.0)
        return round(score, 1)

    # ── Persistence & metrics ──────────────────────────────────────────────────

    def _persist(self, report: MarketDriftReport) -> None:
        """Persiste histórico de drift para rastreabilidade."""
        try:
            DRIFT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":       report.evaluated_at,
                "market_drift_score": report.market_drift_score,
                "edge_decay_score":   report.edge_decay_score,
                "regime_shift_score": report.regime_shift_score,
                "volatility_shift_score": report.volatility_shift_score,
                "fleet_health_avg":   report.fleet_health_avg,
                "dominant_signal":    report.dominant_signal,
            }
            with open(DRIFT_HISTORY_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # never block analysis on persistence failure

    def _emit_metrics(self, report: MarketDriftReport) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_drift.set(report.market_drift_score)
            _prom_edge_decay.set(report.edge_decay_score)
        except Exception:
            pass

    def _empty_report(self, reason: str) -> MarketDriftReport:
        return MarketDriftReport(
            market_drift_score=0.0, regime_shift_score=0.0,
            edge_decay_score=0.0, volatility_shift_score=0.0,
            strategies_analyzed=0, strategies_degraded=0, fleet_health_avg=50.0,
            signals=[DriftSignal("no_data", "low", 0.0, reason)],
            dominant_signal=None,
            recommendation="Execute experimentos para habilitar análise de drift.",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            experiments_total=0,
        )


# ── Drift History Reader ──────────────────────────────────────────────────────

class DriftHistoryReader:
    """Lê e sumariza histórico de drift para análise de tendência."""

    def __init__(self, history_file: Path = DRIFT_HISTORY_FILE):
        self.history_file = history_file

    def load(self, days: int = 30) -> list[dict]:
        if not self.history_file.exists():
            return []
        entries: list[dict] = []
        try:
            with open(self.history_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception:
            return []
        # Últimos `days` dias de entradas (por evaluated_at)
        cutoff = None
        if days:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if cutoff:
            entries = [e for e in entries if e.get("evaluated_at", "") >= cutoff]
        return entries

    def trend(self, days: int = 30) -> dict:
        """Retorna tendência de drift: se está piorando, estabilizando ou melhorando."""
        entries = self.load(days)
        if len(entries) < 2:
            return {"trend": "insufficient_data", "samples": len(entries)}

        scores = [e.get("market_drift_score", 0.0) for e in entries]
        first_half = statistics.mean(scores[: len(scores) // 2])
        second_half = statistics.mean(scores[len(scores) // 2 :])
        delta = second_half - first_half

        if delta > 10:
            trend = "worsening"
        elif delta < -10:
            trend = "improving"
        else:
            trend = "stable"

        return {
            "trend": trend,
            "delta": round(delta, 1),
            "avg_first_half": round(first_half, 1),
            "avg_second_half": round(second_half, 1),
            "samples": len(entries),
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Market Drift Intelligence — Phase N FASE 2"
    )
    parser.add_argument("--json",   action="store_true", help="Output em JSON")
    parser.add_argument("--trend",  action="store_true", help="Mostrar tendência histórica")
    parser.add_argument("--days",   type=int, default=30, help="Janela de histórico (trend)")
    args = parser.parse_args()

    if args.trend:
        reader = DriftHistoryReader()
        t = reader.trend(days=args.days)
        if args.json:
            print(json.dumps(t, indent=2))
        else:
            print(f"\nDrift Trend — últimos {args.days} dias")
            print(f"  Tendência: {t['trend']}")
            print(f"  Delta:     {t.get('delta', 'N/A')}")
            print(f"  Amostras:  {t['samples']}")
        return

    analyzer = MarketDriftIntelligence()
    report   = analyzer.analyze()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"\nMarket Drift Intelligence")
        print(f"  market_drift_score:     {report.market_drift_score:.0f}/100")
        print(f"  edge_decay_score:       {report.edge_decay_score:.0f}/100")
        print(f"  regime_shift_score:     {report.regime_shift_score:.0f}/100")
        print(f"  volatility_shift_score: {report.volatility_shift_score:.0f}/100")
        print(f"  fleet_health_avg:       {report.fleet_health_avg:.0f}/100")
        print(f"  strategies:             {report.strategies_analyzed} ({report.strategies_degraded} degradadas)")
        if report.signals:
            print("  Sinais:")
            for s in report.signals:
                print(f"    [{s.severity.upper()}] {s.description}")
        print(f"\n  → {report.recommendation}")


if __name__ == "__main__":
    main()

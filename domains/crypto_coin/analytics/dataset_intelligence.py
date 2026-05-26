"""
dataset_intelligence.py — Phase L FASE 11

Inteligência de datasets OHLCV — análise de confiabilidade por exchange/pair.

Complementa DatasetQA (Phase I) com análise temporal e de confiabilidade:
  - Exchange reliability ranking (qual exchange tem dados mais confiáveis)
  - Pair reliability ranking (qual par tem histórico mais íntegro)
  - Replay reliability score (confiabilidade do par para backtesting)
  - Drift persistence analysis (candles desviando historicamente)

Entrada: resultados de ohlcv_integrity + DatasetQA armazenados
Saída:   ranking + alertas de qualidade por exchange/par

Princípio: reutiliza check_integrity() de ohlcv_integrity.py — apenas agrega.

CLI:
  python -m domains.crypto_coin.analytics.dataset_intelligence --exchange binance
  python -m domains.crypto_coin.analytics.dataset_intelligence --pair BTC/USDT --tf 15m
  python -m domains.crypto_coin.analytics.dataset_intelligence --reliability-report
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Interfaces ────────────────────────────────────────────────────────────────

@dataclass
class PairReliabilityScore:
    symbol:         str
    timeframe:      str
    exchange:       str
    integrity_score: float   # 0–100 (de ohlcv_integrity)
    gap_count:      int
    anomaly_count:  int
    coverage_days:  int      # quantos dias de dados disponíveis
    replay_reliable: bool    # adequado para backtesting
    reliability_class: str   # 'excellent' | 'good' | 'fair' | 'poor'
    notes:          list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

@dataclass
class ExchangeReliabilityRank:
    exchange:       str
    avg_score:      float
    total_pairs:    int
    excellent_count: int
    poor_count:     int
    rank:           int
    computed_at:    str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

@dataclass
class DriftPersistenceReport:
    symbol:          str
    timeframe:       str
    drift_windows:   list[dict]   # {period, drift_magnitude, direction}
    persistent_drift: bool        # drift que persiste >7 dias
    drift_severity:  str          # 'none' | 'low' | 'medium' | 'high'
    recommendation:  str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


# ── Dataset Intelligence ──────────────────────────────────────────────────────

class DatasetIntelligence:
    """
    Analisa a confiabilidade de datasets OHLCV para backtesting.
    """

    def __init__(self, db=None):
        self._db = db

    # ── Reliability por par ───────────────────────────────────────────────────

    def get_pair_reliability(
        self,
        symbol:    str,
        timeframe: str,
        days:      int = 90,
        exchange:  str = "binance",
    ) -> PairReliabilityScore:
        """
        Calcula o score de confiabilidade de um par para backtesting.
        Executa check_integrity() e adiciona métricas de coverage.
        """
        try:
            from .ohlcv_integrity import check_integrity
            result = check_integrity(self._get_db(), symbol, timeframe, days=days)
            integrity_score = getattr(result, "integrity_score", 0.0)
            gap_count       = getattr(result, "gap_count", 0)
            anomaly_count   = getattr(result, "anomaly_count", 0)
            coverage_days   = getattr(result, "coverage_days", days)
        except Exception as e:
            logger.warning(f"check_integrity falhou para {symbol}/{timeframe}: {e}")
            integrity_score = 0.0
            gap_count       = 0
            anomaly_count   = 0
            coverage_days   = 0

        reliability_class = (
            "excellent" if integrity_score >= 90
            else "good" if integrity_score >= 75
            else "fair" if integrity_score >= 60
            else "poor"
        )

        replay_reliable = integrity_score >= 75 and gap_count < 10

        notes = []
        if gap_count > 20:
            notes.append(f"Alto número de gaps ({gap_count}) — pode distorcer backtests")
        if anomaly_count > 5:
            notes.append(f"Anomalias detectadas ({anomaly_count}) — validar candles suspeitos")
        if coverage_days < days * 0.8:
            notes.append(f"Cobertura incompleta ({coverage_days}/{days} dias)")
        if integrity_score >= 90:
            notes.append("Dataset de alta qualidade — recomendado para pesquisa")

        return PairReliabilityScore(
            symbol            = symbol,
            timeframe         = timeframe,
            exchange          = exchange,
            integrity_score   = round(integrity_score, 1),
            gap_count         = gap_count,
            anomaly_count     = anomaly_count,
            coverage_days     = coverage_days,
            replay_reliable   = replay_reliable,
            reliability_class = reliability_class,
            notes             = notes,
        )

    # ── Ranking por exchange ──────────────────────────────────────────────────

    def rank_exchanges(
        self,
        pairs:     list[tuple[str, str]],  # [(symbol, timeframe), ...]
        exchanges: list[str] | None = None,
        days:      int = 90,
    ) -> list[ExchangeReliabilityRank]:
        """
        Compara exchanges baseado na integridade de seus datasets.
        Por padrão usa apenas 'binance' (única exchange no data-core).
        """
        # No data-core atual há apenas binance — estrutura preparada para expansão
        available_exchanges = exchanges or ["binance"]
        ranks: list[ExchangeReliabilityRank] = []

        for rank, exchange in enumerate(available_exchanges, start=1):
            scores = []
            excellent = poor = 0

            for symbol, tf in pairs:
                r = self.get_pair_reliability(symbol, tf, days=days, exchange=exchange)
                scores.append(r.integrity_score)
                if r.reliability_class == "excellent": excellent += 1
                if r.reliability_class == "poor":       poor += 1

            avg = sum(scores) / len(scores) if scores else 0.0

            ranks.append(ExchangeReliabilityRank(
                exchange       = exchange,
                avg_score      = round(avg, 1),
                total_pairs    = len(pairs),
                excellent_count= excellent,
                poor_count     = poor,
                rank           = rank,
                computed_at    = datetime.now(timezone.utc).isoformat(),
            ))

        # Re-rank por avg_score
        ranks.sort(key=lambda r: r.avg_score, reverse=True)
        for i, r in enumerate(ranks):
            r.rank = i + 1

        return ranks

    # ── Drift persistence ─────────────────────────────────────────────────────

    def analyze_drift_persistence(
        self,
        symbol:    str,
        timeframe: str,
        days:      int = 90,
    ) -> DriftPersistenceReport:
        """
        Analisa se há drift persistente nos preços (desvio sistemático das médias).
        Drift persistente pode distorcer backtests e invalidar comparações históricas.
        """
        # Tentar usar ohlcv_integrity para pegar métricas de drift
        drift_windows: list[dict] = []
        persistent_drift = False
        drift_severity   = "none"

        try:
            from .ohlcv_integrity import check_integrity
            result = check_integrity(self._get_db(), symbol, timeframe, days=days)

            # Verificar se o resultado tem campos de drift (adicionado em Phase G)
            if hasattr(result, "drift_score") and result.drift_score is not None:
                drift_score = result.drift_score
                if drift_score > 0.3:
                    drift_windows.append({
                        "period": f"últimos {days} dias",
                        "drift_magnitude": round(drift_score, 3),
                        "direction": "positive" if getattr(result, "drift_direction", 0) > 0 else "negative",
                    })
                    persistent_drift = drift_score > 0.5
                    drift_severity = "high" if drift_score > 0.7 else "medium" if drift_score > 0.4 else "low"

        except Exception as e:
            logger.debug(f"Drift analysis falhou para {symbol}: {e}")

        if drift_severity == "none":
            recommendation = "Dataset estável — sem drift detectado. Adequado para backtesting."
        elif drift_severity == "low":
            recommendation = "Drift baixo detectado. Monitorar; impacto mínimo em backtests."
        elif drift_severity == "medium":
            recommendation = "Drift moderado. Usar dados com cautela; validar período específico."
        else:
            recommendation = "Drift persistente alto. Recomendado re-normalizar dados antes de backtesting."

        return DriftPersistenceReport(
            symbol           = symbol,
            timeframe        = timeframe,
            drift_windows    = drift_windows,
            persistent_drift = persistent_drift,
            drift_severity   = drift_severity,
            recommendation   = recommendation,
        )

    # ── Reliability report completo ───────────────────────────────────────────

    def full_reliability_report(
        self,
        pairs: list[tuple[str, str]],
        days:  int = 90,
    ) -> dict[str, Any]:
        """
        Relatório completo de confiabilidade da frota de datasets.
        """
        pair_scores = [
            self.get_pair_reliability(sym, tf, days=days).to_dict()
            for sym, tf in pairs
        ]

        excellent = sum(1 for p in pair_scores if p["reliability_class"] == "excellent")
        good      = sum(1 for p in pair_scores if p["reliability_class"] == "good")
        fair      = sum(1 for p in pair_scores if p["reliability_class"] == "fair")
        poor      = sum(1 for p in pair_scores if p["reliability_class"] == "poor")

        avg_score = sum(p["integrity_score"] for p in pair_scores) / max(len(pair_scores), 1)

        return {
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "pairs_analyzed":  len(pairs),
            "days_window":     days,
            "fleet_avg_score": round(avg_score, 1),
            "distribution":    {"excellent": excellent, "good": good, "fair": fair, "poor": poor},
            "pairs":           pair_scores,
        }

    # ── DB helper ─────────────────────────────────────────────────────────────

    def _get_db(self):
        if self._db:
            return self._db
        try:
            from db.connection import get_db_connection
            return get_db_connection()
        except Exception:
            return None


# ── CLI ───────────────────────────────────────────────────────────────────────

_DEFAULT_PAIRS = [
    ("BTC/USDT", "15m"),
    ("ETH/USDT", "15m"),
    ("BNB/USDT", "15m"),
]

def main() -> None:
    parser = argparse.ArgumentParser(description="Dataset Intelligence")
    parser.add_argument("--pair",      help="Par no formato SYMBOL/QUOTE (ex: BTC/USDT)")
    parser.add_argument("--tf",        default="15m")
    parser.add_argument("--days",      type=int, default=90)
    parser.add_argument("--exchange",  default="binance")
    parser.add_argument("--reliability-report", action="store_true", dest="full_report")
    parser.add_argument("--drift",     action="store_true")
    parser.add_argument("--json",      action="store_true")
    args = parser.parse_args()

    intel = DatasetIntelligence()

    if args.full_report:
        report = intel.full_reliability_report(_DEFAULT_PAIRS, days=args.days)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"\n📊 Dataset Reliability Report")
            print(f"   Fleet avg score: {report['fleet_avg_score']}/100")
            print(f"   Distribution: {report['distribution']}")
            for p in report["pairs"]:
                star = "✅" if p["reliability_class"] in ("excellent", "good") else "⚠️"
                print(f"   {star} {p['symbol']}/{p['timeframe']}: {p['integrity_score']:.1f} ({p['reliability_class']})")
        return

    if args.pair:
        r = intel.get_pair_reliability(args.pair, args.tf, days=args.days, exchange=args.exchange)
        if args.json:
            print(json.dumps(r.to_dict(), indent=2))
        else:
            print(f"\n📈 {r.symbol} / {r.timeframe} — {r.exchange}")
            print(f"   Score:    {r.integrity_score}/100 ({r.reliability_class})")
            print(f"   Gaps:     {r.gap_count}")
            print(f"   Anomalias: {r.anomaly_count}")
            print(f"   Coverage: {r.coverage_days} dias")
            print(f"   Replay:   {'✅ Confiável' if r.replay_reliable else '⚠️ Com ressalvas'}")
            for note in r.notes:
                print(f"   ℹ️  {note}")

        if args.drift:
            d = intel.analyze_drift_persistence(args.pair, args.tf, days=args.days)
            print(f"\n   Drift: {d.drift_severity} — {d.recommendation}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()

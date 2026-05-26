"""
dataset_qa.py — Phase I Fase 10

Controle de qualidade global dos datasets OHLCV armazenados no banco de dados.

Executa `check_integrity()` para todos os pares símbolo/timeframe e produz:
  - QualityRanking: lista de símbolos/tf ordenados por integrity_score
  - DatasetQASummary: visão geral da frota (pares, score médio, críticos)
  - Relatório de priorização para coleta/re-download

Critérios de classificação:
  CLEAN       integrity_score >= 95
  ACCEPTABLE  integrity_score >= 80
  DEGRADED    integrity_score >= 60
  CRITICAL    integrity_score <  60

CLI:
    python -m domains.crypto_coin.analytics.dataset_qa --all
    python -m domains.crypto_coin.analytics.dataset_qa --symbol BTC/USDT --tf 15m
    python -m domains.crypto_coin.analytics.dataset_qa --critical-only
    python -m domains.crypto_coin.analytics.dataset_qa --json
    python -m domains.crypto_coin.analytics.dataset_qa --days 60
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from .ohlcv_integrity import check_integrity, check_all_symbols, OHLCVIntegrityReport


# ── Classification ────────────────────────────────────────────────────────────

def _classify(score: float) -> str:
    if score >= 95:
        return "CLEAN"
    if score >= 80:
        return "ACCEPTABLE"
    if score >= 60:
        return "DEGRADED"
    return "CRITICAL"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class DatasetQAEntry:
    symbol:          str
    timeframe:       str
    integrity_score: float
    quality_class:   str     # CLEAN | ACCEPTABLE | DEGRADED | CRITICAL
    completeness_pct: float
    gap_count:       int
    total_missing:   int
    anomaly_count:   int
    flat_candles:    int
    drift_count:     int
    total_candles:   int
    expected_candles: int
    status:          str     # status original do OHLCVIntegrityReport

    def to_dict(self) -> dict:
        return {
            "symbol":           self.symbol,
            "timeframe":        self.timeframe,
            "integrity_score":  round(self.integrity_score, 2),
            "quality_class":    self.quality_class,
            "completeness_pct": round(self.completeness_pct, 2),
            "gap_count":        self.gap_count,
            "total_missing":    self.total_missing,
            "anomaly_count":    self.anomaly_count,
            "flat_candles":     self.flat_candles,
            "drift_count":      self.drift_count,
            "total_candles":    self.total_candles,
            "expected_candles": self.expected_candles,
            "status":           self.status,
        }


@dataclass
class DatasetQASummary:
    total_pairs:     int
    avg_score:       float
    median_score:    float
    clean_count:     int
    acceptable_count: int
    degraded_count:  int
    critical_count:  int
    entries:         list[DatasetQAEntry] = field(default_factory=list)

    @property
    def critical_pairs(self) -> list[DatasetQAEntry]:
        return [e for e in self.entries if e.quality_class == "CRITICAL"]

    @property
    def degraded_pairs(self) -> list[DatasetQAEntry]:
        return [e for e in self.entries if e.quality_class == "DEGRADED"]

    def summary_text(self) -> str:
        lines = [
            "Dataset QA Summary",
            f"  Total pares     : {self.total_pairs}",
            f"  Score médio     : {self.avg_score:.1f}/100",
            f"  Score mediano   : {self.median_score:.1f}/100",
            f"  CLEAN           : {self.clean_count}",
            f"  ACCEPTABLE      : {self.acceptable_count}",
            f"  DEGRADED        : {self.degraded_count}",
            f"  CRITICAL        : {self.critical_count}",
        ]

        if self.critical_pairs:
            lines.append("\n  🔴 Pares CRÍTICOS (precisam de re-download):")
            for e in self.critical_pairs:
                lines.append(
                    f"    {e.symbol:<12} [{e.timeframe}] "
                    f"score={e.integrity_score:.1f}  "
                    f"completeness={e.completeness_pct:.1f}%  "
                    f"gaps={e.gap_count}  anomalies={e.anomaly_count}"
                )

        if self.degraded_pairs:
            lines.append("\n  🟡 Pares DEGRADADOS (monitorar):")
            for e in self.degraded_pairs[:10]:
                lines.append(
                    f"    {e.symbol:<12} [{e.timeframe}] "
                    f"score={e.integrity_score:.1f}  "
                    f"completeness={e.completeness_pct:.1f}%"
                )

        return "\n".join(lines)

    def quality_ranking(self, n: int = 20) -> str:
        """Tabela de ranking dos N piores pares."""
        sorted_entries = sorted(self.entries, key=lambda e: e.integrity_score)[:n]
        if not sorted_entries:
            return "Nenhum par encontrado."
        header = f"{'#':>3}  {'Symbol':<12}  {'TF':>5}  {'Score':>6}  {'Class':<12}  {'Complete':>9}  {'Gaps':>5}  {'Anomalies':>10}"
        lines  = [header, "-" * len(header)]
        for i, e in enumerate(sorted_entries, 1):
            lines.append(
                f"{i:>3}  {e.symbol:<12}  {e.timeframe:>5}  "
                f"{e.integrity_score:>6.1f}  {e.quality_class:<12}  "
                f"{e.completeness_pct:>8.1f}%  {e.gap_count:>5}  {e.anomaly_count:>10}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_pairs":      self.total_pairs,
            "avg_score":        round(self.avg_score, 2),
            "median_score":     round(self.median_score, 2),
            "clean_count":      self.clean_count,
            "acceptable_count": self.acceptable_count,
            "degraded_count":   self.degraded_count,
            "critical_count":   self.critical_count,
            "entries":          [e.to_dict() for e in self.entries],
        }


# ── QA Functions ──────────────────────────────────────────────────────────────

def run_dataset_qa(
    db: Session,
    *,
    symbol:    str | None = None,
    timeframe: str | None = None,
    days:      int        = 30,
    source:    str        = "binance",
) -> DatasetQASummary:
    """
    Executa QA completo dos datasets OHLCV.

    Se symbol/timeframe fornecidos: verifica apenas esse par.
    Caso contrário: verifica todos os pares existentes no DB.
    """
    if symbol and timeframe:
        reports = [check_integrity(db, symbol=symbol, timeframe=timeframe,
                                   days=days, source=source)]
    else:
        reports = check_all_symbols(db, days=days, source=source)

    entries = [_report_to_entry(r) for r in reports]
    return _build_summary(entries)


def _report_to_entry(r: OHLCVIntegrityReport) -> DatasetQAEntry:
    score = r.integrity_score
    return DatasetQAEntry(
        symbol=r.symbol,
        timeframe=r.timeframe,
        integrity_score=score,
        quality_class=_classify(score),
        completeness_pct=r.completeness_pct,
        gap_count=r.gap_count,
        total_missing=r.total_missing,
        anomaly_count=r.anomaly_count,
        flat_candles=r.flat_candles_count,
        drift_count=r.timeframe_drift_count,
        total_candles=r.total_candles,
        expected_candles=r.expected_candles,
        status=r.status,
    )


def _build_summary(entries: list[DatasetQAEntry]) -> DatasetQASummary:
    n = len(entries)
    if n == 0:
        return DatasetQASummary(
            total_pairs=0, avg_score=0.0, median_score=0.0,
            clean_count=0, acceptable_count=0, degraded_count=0, critical_count=0,
        )

    scores = sorted(e.integrity_score for e in entries)
    avg_score    = sum(scores) / n
    median_score = scores[n // 2]

    clean        = sum(1 for e in entries if e.quality_class == "CLEAN")
    acceptable   = sum(1 for e in entries if e.quality_class == "ACCEPTABLE")
    degraded     = sum(1 for e in entries if e.quality_class == "DEGRADED")
    critical     = sum(1 for e in entries if e.quality_class == "CRITICAL")

    # Ordenar por score (piores primeiro) para relatório
    entries_sorted = sorted(entries, key=lambda e: e.integrity_score)

    summary = DatasetQASummary(
        total_pairs=n,
        avg_score=avg_score,
        median_score=median_score,
        clean_count=clean,
        acceptable_count=acceptable,
        degraded_count=degraded,
        critical_count=critical,
        entries=entries_sorted,
    )

    # Phase K FASE 11 — Expor métricas de frota no Prometheus
    _emit_fleet_prometheus_metrics(summary)

    return summary


def _emit_fleet_prometheus_metrics(summary: DatasetQASummary) -> None:
    """
    Atualiza as Gauges de fleet QA no Prometheus.
    Chamado automaticamente ao final de _build_summary().
    Falha silenciosamente (import pode estar ausente em contextos de teste).
    """
    try:
        from api import metrics as prom_metrics
        prom_metrics.dataset_qa_fleet_score.set(round(summary.avg_score, 2))
        prom_metrics.dataset_qa_critical_count.set(summary.critical_count)
    except Exception:  # noqa: BLE001
        pass  # Prometheus não disponível — não quebrar o QA


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dataset QA — controle de qualidade OHLCV")
    parser.add_argument("--all",           action="store_true", help="Verificar todos os pares")
    parser.add_argument("--symbol",        default=None, help="Símbolo específico (ex: BTC/USDT)")
    parser.add_argument("--tf",            default=None, dest="timeframe", help="Timeframe (ex: 15m)")
    parser.add_argument("--days",          type=int,   default=30, help="Janela de verificação em dias")
    parser.add_argument("--source",        default="binance")
    parser.add_argument("--critical-only", action="store_true", help="Mostrar apenas pares críticos")
    parser.add_argument("--ranking",       type=int,   default=None, help="Mostrar ranking dos N piores")
    parser.add_argument("--json",          action="store_true", help="Saída em JSON")
    args = parser.parse_args()

    from database.session import SessionLocal
    db = SessionLocal()
    try:
        summary = run_dataset_qa(
            db,
            symbol=args.symbol,
            timeframe=args.timeframe,
            days=args.days,
            source=args.source,
        )

        if args.json:
            print(json.dumps(summary.to_dict(), indent=2))
        elif args.critical_only:
            print(summary.summary_text())
        elif args.ranking:
            print(summary.quality_ranking(n=args.ranking))
        else:
            print(summary.summary_text())
            print()
            print(summary.quality_ranking(n=20))
    finally:
        db.close()

"""
OHLCV Integrity Checker
========================
Valida os dados históricos de candles armazenados em `normalized_market_candles`
(Postgres) para detectar gaps temporais, duplicatas, anomalias de preço e
volume zero.

Uso standalone:
    python -m domains.crypto_coin.analytics.ohlcv_integrity --symbol BTC/USDT --tf 15m
    python -m domains.crypto_coin.analytics.ohlcv_integrity --all --days 30

Uso programático:
    from domains.crypto_coin.analytics.ohlcv_integrity import OHLCVIntegrityReport, check_integrity
    report = check_integrity(db, symbol="BTC/USDT", timeframe="15m", days=90)
    print(report.summary())
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.normalization.models import NormalizedMarketCandle

# Prometheus metrics — wire G-H-04 (Phase H Fase 11)
def _get_integrity_metrics():
    try:
        from api.metrics import ohlcv_integrity_checks_total, ohlcv_gaps_detected_total
        return ohlcv_integrity_checks_total, ohlcv_gaps_detected_total
    except Exception:
        return None, None

# Segundos por timeframe
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400,
}

# Limites para anomalia de preço (%)
MAX_CANDLE_MOVE_PCT = 20.0   # variação close→close > 20% é suspeita
# Limites para OHLC sanity
HLOC_TOLERANCE = 0.001       # high deve ser ≥ low (com tolerância de 0.1%)


@dataclass
class GapRecord:
    prev_timestamp: datetime
    next_timestamp: datetime
    gap_seconds: int
    expected_seconds: int
    missing_candles: int


@dataclass
class AnomalyRecord:
    timestamp: datetime
    kind: str          # 'zero_volume' | 'ohlc_invalid' | 'price_spike' | 'duplicate'
    detail: str


@dataclass
class OHLCVIntegrityReport:
    symbol: str
    timeframe: str
    period_start: datetime
    period_end: datetime
    total_candles: int
    expected_candles: int
    completeness_pct: float
    gaps: list[GapRecord] = field(default_factory=list)
    anomalies: list[AnomalyRecord] = field(default_factory=list)
    # Phase H Fase 10 — extended fields
    timeframe_drift_count: int = 0       # candles com drift > 10% do tf esperado
    flat_candles_count:    int = 0       # candles com open==high==low==close (dados suspeitos)

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    @property
    def total_missing(self) -> int:
        return sum(g.missing_candles for g in self.gaps)

    @property
    def anomaly_count(self) -> int:
        return len(self.anomalies)

    @property
    def integrity_score(self) -> float:
        """
        Score de integridade 0–100.
        100 = dataset perfeito; penaliza gaps, anomalias, drift, flat candles.
        Phase H Fase 10.
        """
        base = self.completeness_pct  # 0–100
        # Penalidades
        gap_penalty      = min(30.0, self.gap_count * 2.0)
        anomaly_penalty  = min(20.0, self.anomaly_count * 1.5)
        drift_penalty    = min(10.0, self.timeframe_drift_count * 0.5)
        flat_penalty     = min(5.0,  self.flat_candles_count * 0.2)
        score = base - gap_penalty - anomaly_penalty - drift_penalty - flat_penalty
        return round(max(0.0, min(100.0, score)), 2)

    @property
    def status(self) -> str:
        if self.completeness_pct >= 99.0 and self.anomaly_count == 0:
            return "CLEAN"
        if self.completeness_pct >= 95.0 and self.anomaly_count <= 5:
            return "ACCEPTABLE"
        if self.completeness_pct >= 85.0:
            return "DEGRADED"
        return "CRITICAL"

    def summary(self) -> str:
        lines = [
            f"OHLCV Integrity — {self.symbol} [{self.timeframe}]",
            f"  Período    : {self.period_start.date()} → {self.period_end.date()}",
            f"  Candles    : {self.total_candles:,} / {self.expected_candles:,} esperados"
            f"  ({self.completeness_pct:.1f}%)",
            f"  Gaps       : {self.gap_count} ({self.total_missing} candles ausentes)",
            f"  Anomalias  : {self.anomaly_count}",
            f"  Drift TF   : {self.timeframe_drift_count}",
            f"  Flat candles: {self.flat_candles_count}",
            f"  Score      : {self.integrity_score:.1f}/100",
            f"  Status     : {self.status}",
        ]
        if self.gaps:
            lines.append("\n  Maiores gaps:")
            for g in sorted(self.gaps, key=lambda x: x.missing_candles, reverse=True)[:5]:
                lines.append(
                    f"    {g.prev_timestamp.isoformat()} → {g.next_timestamp.isoformat()}"
                    f"  ({g.missing_candles} candles)"
                )
        if self.anomalies:
            lines.append("\n  Anomalias:")
            for a in self.anomalies[:10]:
                lines.append(f"    [{a.kind}] {a.timestamp.isoformat()} — {a.detail}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "symbol":               self.symbol,
            "timeframe":            self.timeframe,
            "period_start":         self.period_start.isoformat(),
            "period_end":           self.period_end.isoformat(),
            "total_candles":        self.total_candles,
            "expected_candles":     self.expected_candles,
            "completeness_pct":     self.completeness_pct,
            "gap_count":            self.gap_count,
            "total_missing":        self.total_missing,
            "anomaly_count":        self.anomaly_count,
            "timeframe_drift_count": self.timeframe_drift_count,
            "flat_candles_count":   self.flat_candles_count,
            "integrity_score":      self.integrity_score,
            "status":               self.status,
            "gaps": [
                {
                    "from": g.prev_timestamp.isoformat(),
                    "to":   g.next_timestamp.isoformat(),
                    "missing": g.missing_candles,
                }
                for g in self.gaps
            ],
            "anomalies": [
                {"timestamp": a.timestamp.isoformat(), "kind": a.kind, "detail": a.detail}
                for a in self.anomalies
            ],
        }


def check_integrity(
    db: Session,
    *,
    symbol: str,
    timeframe: str,
    days: int = 90,
    source: str = "binance",
) -> OHLCVIntegrityReport:
    """
    Verifica a integridade dos candles OHLCV no banco de dados.

    Retorna um `OHLCVIntegrityReport` com gaps, anomalias e status geral.
    """
    tf_secs = TIMEFRAME_SECONDS.get(timeframe, 900)
    since   = datetime.now(tz=timezone.utc) - timedelta(days=days)

    candles = (
        db.query(NormalizedMarketCandle)
        .filter(
            NormalizedMarketCandle.source == source,
            NormalizedMarketCandle.symbol == symbol,
            NormalizedMarketCandle.timeframe == timeframe,
            NormalizedMarketCandle.timestamp >= since,
        )
        .order_by(NormalizedMarketCandle.timestamp)
        .all()
    )

    total        = len(candles)
    period_start = candles[0].timestamp  if candles else since
    period_end   = candles[-1].timestamp if candles else datetime.now(tz=timezone.utc)

    # Candles esperados no período
    period_secs   = (period_end - period_start).total_seconds()
    expected      = max(1, int(period_secs / tf_secs) + 1)
    completeness  = round(total / expected * 100, 2) if expected > 0 else 0.0

    gaps:      list[GapRecord]     = []
    anomalies: list[AnomalyRecord] = []
    seen_ts:   set[datetime]       = set()

    prev_close:   Optional[float] = None
    drift_count:  int             = 0
    flat_count:   int             = 0

    for i, c in enumerate(candles):
        ts = c.timestamp

        # ── Duplicata ──────────────────────────────────────────
        if ts in seen_ts:
            anomalies.append(AnomalyRecord(
                timestamp=ts,
                kind="duplicate",
                detail=f"Timestamp duplicado: {ts.isoformat()}",
            ))
            continue
        seen_ts.add(ts)

        # ── Gap temporal ───────────────────────────────────────
        if i > 0:
            prev_ts   = candles[i - 1].timestamp
            delta     = int((ts - prev_ts).total_seconds())
            if delta > tf_secs * 1.5:  # tolerância de 50%
                missing = int(delta / tf_secs) - 1
                gaps.append(GapRecord(
                    prev_timestamp=prev_ts,
                    next_timestamp=ts,
                    gap_seconds=delta,
                    expected_seconds=tf_secs,
                    missing_candles=missing,
                ))
            # ── Timeframe drift (Phase H Fase 10) ──────────────
            # Candle chegou mais cedo/tarde do que o esperado (drift > 10% do tf)
            elif abs(delta - tf_secs) > tf_secs * 0.10:
                drift_count += 1

        # ── OHLC sanity ────────────────────────────────────────
        try:
            o = float(c.open)
            h = float(c.high)
            lo = float(c.low)
            cl = float(c.close)

            if h < lo * (1 - HLOC_TOLERANCE):
                anomalies.append(AnomalyRecord(
                    timestamp=ts,
                    kind="ohlc_invalid",
                    detail=f"High({h}) < Low({lo})",
                ))
            if cl > h * (1 + HLOC_TOLERANCE) or cl < lo * (1 - HLOC_TOLERANCE):
                anomalies.append(AnomalyRecord(
                    timestamp=ts,
                    kind="ohlc_invalid",
                    detail=f"Close({cl}) fora do range [Low={lo}, High={h}]",
                ))
        except (TypeError, ValueError):
            anomalies.append(AnomalyRecord(
                timestamp=ts,
                kind="ohlc_invalid",
                detail="OHLC com valor None ou não numérico",
            ))
            continue

        # ── Volume zero ────────────────────────────────────────
        try:
            vol = float(c.volume) if c.volume is not None else None
            if vol is not None and vol == 0.0:
                anomalies.append(AnomalyRecord(
                    timestamp=ts,
                    kind="zero_volume",
                    detail=f"Volume=0 em {ts.isoformat()}",
                ))
        except (TypeError, ValueError):
            pass

        # ── Price spike ────────────────────────────────────────
        if prev_close is not None and prev_close > 0:
            move_pct = abs((cl - prev_close) / prev_close) * 100
            if move_pct > MAX_CANDLE_MOVE_PCT:
                anomalies.append(AnomalyRecord(
                    timestamp=ts,
                    kind="price_spike",
                    detail=f"Variação {move_pct:.1f}% (close {prev_close:.2f} → {cl:.2f})",
                ))
        # ── Flat candle detection (Phase H Fase 10) ───────────
        # Candle onde open == high == low == close — dados suspeitos (estagnação ou erro de feed)
        try:
            if abs(o - h) < 1e-10 and abs(h - lo) < 1e-10 and abs(lo - cl) < 1e-10:
                flat_count += 1
                anomalies.append(AnomalyRecord(
                    timestamp=ts,
                    kind="flat_candle",
                    detail=f"Flat candle: O=H=L=C={cl:.6f}",
                ))
        except Exception:
            pass

        prev_close = cl

    report = OHLCVIntegrityReport(
        symbol=symbol,
        timeframe=timeframe,
        period_start=period_start,
        period_end=period_end,
        total_candles=total,
        expected_candles=expected,
        completeness_pct=completeness,
        gaps=gaps,
        anomalies=anomalies,
        timeframe_drift_count=drift_count,
        flat_candles_count=flat_count,
    )

    # ── Wire Prometheus metrics — G-H-04 fix (Phase H Fase 11) ──────────────
    _integrity_counter, _gaps_counter = _get_integrity_metrics()
    if _integrity_counter is not None:
        try:
            _integrity_counter.labels(
                symbol=symbol, timeframe=timeframe, status=report.status
            ).inc()
            if _gaps_counter and report.gap_count > 0:
                _gaps_counter.labels(symbol=symbol, timeframe=timeframe).inc(report.gap_count)
        except Exception:
            pass

    return report


def check_all_symbols(
    db: Session,
    *,
    days: int = 30,
    source: str = "binance",
) -> list[OHLCVIntegrityReport]:
    """Verifica todos os símbolos/timeframes existentes no banco."""
    from sqlalchemy import func, distinct

    pairs = (
        db.query(
            distinct(NormalizedMarketCandle.symbol),
            NormalizedMarketCandle.timeframe,
        )
        .filter(NormalizedMarketCandle.source == source)
        .all()
    )

    reports = []
    for symbol, timeframe in pairs:
        report = check_integrity(db, symbol=symbol, timeframe=timeframe,
                                 days=days, source=source)
        reports.append(report)
    return reports


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    parser = argparse.ArgumentParser(description="OHLCV Integrity Checker")
    parser.add_argument("--symbol",  default=None, help="ex: BTC/USDT")
    parser.add_argument("--tf",      default="15m", dest="timeframe")
    parser.add_argument("--source",  default="binance")
    parser.add_argument("--days",    type=int, default=30)
    parser.add_argument("--all",     action="store_true", help="Verifica todos os pares")
    parser.add_argument("--json",    action="store_true", help="Saída em JSON")
    args = parser.parse_args()

    from database.session import SessionLocal
    db = SessionLocal()
    try:
        if args.all:
            reports = check_all_symbols(db, days=args.days, source=args.source)
        elif args.symbol:
            reports = [check_integrity(db, symbol=args.symbol, timeframe=args.timeframe,
                                       days=args.days, source=args.source)]
        else:
            print("Use --symbol BTC/USDT ou --all", file=sys.stderr)
            sys.exit(1)

        if args.json:
            print(json.dumps([r.to_dict() for r in reports], indent=2, default=str))
        else:
            for r in reports:
                print(r.summary())
                print()
    finally:
        db.close()

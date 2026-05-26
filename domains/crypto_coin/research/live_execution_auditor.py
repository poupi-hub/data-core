"""
live_execution_auditor.py — Phase Q Q-2

Live Execution Auditor.

Compara execucao paper vs live e calcula metricas de qualidade de execucao:
  - slippage_real:       deslizamento entre preco esperado e executado (bps)
  - spread_impact:       impacto do spread no resultado final
  - fill_quality:        qualidade de preenchimento (1.0 = fill completo)
  - execution_latency:   tempo entre sinal e confirmacao (ms)
  - order_efficiency:    eficiencia de uso da ordem
  - execution_drift:     deriva acumulada entre paper e live

Detecta:
  - slippage_deterioration:   slippage medio aumentando ao longo do tempo
  - fill_inconsistency:       fills muito abaixo do esperado
  - latency_spike:            latencia fora do range normal
  - exchange_degradation:     multiplos sinais de degradacao simultaneos

Score produzido:
  - execution_quality_score (0-100)

CLI:
  python -m domains.crypto_coin.research.live_execution_auditor
  python -m domains.crypto_coin.research.live_execution_auditor --json
  python -m domains.crypto_coin.research.live_execution_auditor --record
"""

from __future__ import annotations

import argparse
import json
import statistics
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

AUDIT_LOG      = Path("data/live_execution_audit_log.jsonl")
EXEC_AUDIT_SUMMARY = Path("data/live_execution_audit_summary.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import (
        execution_quality_score as _prom_quality,
        live_slippage_bps       as _prom_slippage,
        execution_latency_ms    as _prom_latency,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Thresholds ─────────────────────────────────────────────────────────────────

SLIPPAGE_WARN_BPS    = 8.0    # slippage medio > 8bps = warning
SLIPPAGE_CRIT_BPS    = 20.0   # slippage medio > 20bps = critico
FILL_QUALITY_WARN    = 0.80   # fill medio < 80% = warning
FILL_QUALITY_CRIT    = 0.60   # fill medio < 60% = critico
LATENCY_WARN_MS      = 500    # latencia media > 500ms = warning
LATENCY_CRIT_MS      = 1500   # latencia media > 1500ms = critico
MIN_SAMPLES_AUDIT    = 3      # minimo de execucoes para auditoria valida


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ExecutionRecord:
    """Registro de uma execucao individual (paper ou live)."""
    record_id:        str
    mode:             str   # paper | live
    symbol:           str
    side:             str   # buy | sell
    expected_price:   float
    executed_price:   float
    requested_size:   float
    filled_size:      float
    latency_ms:       float
    fee_usd:          float
    slippage_bps:     float   # calculado automaticamente
    fill_rate:        float   # filled_size / requested_size
    recorded_at:      str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def build(
        mode: str, symbol: str, side: str,
        expected_price: float, executed_price: float,
        requested_size: float, filled_size: float,
        latency_ms: float, fee_usd: float,
    ) -> "ExecutionRecord":
        slippage_bps = abs(executed_price - expected_price) / expected_price * 10000
        fill_rate    = filled_size / max(requested_size, 1e-9)
        return ExecutionRecord(
            record_id      = str(uuid.uuid4())[:10],
            mode           = mode,
            symbol         = symbol,
            side           = side,
            expected_price = expected_price,
            executed_price = executed_price,
            requested_size = requested_size,
            filled_size    = filled_size,
            latency_ms     = latency_ms,
            fee_usd        = fee_usd,
            slippage_bps   = round(slippage_bps, 2),
            fill_rate      = round(fill_rate, 4),
            recorded_at    = datetime.now(timezone.utc).isoformat(),
        )


@dataclass
class ExecutionAnomalies:
    slippage_deterioration: bool
    fill_inconsistency:     bool
    latency_spike:          bool
    exchange_degradation:   bool
    anomaly_count:          int


@dataclass
class ExecutionAuditReport:
    """Relatorio de qualidade de execucao live."""
    execution_quality_score: float   # 0-100

    # Metricas live
    avg_slippage_bps:    float
    avg_fill_quality:    float
    avg_latency_ms:      float
    avg_spread_impact:   float
    execution_drift:     float   # diferenca acumulada paper vs live PnL

    # Samples
    live_executions:     int
    paper_executions:    int
    audit_window:        int   # numero de execucoes analisadas

    # Anomalias
    anomalies:           ExecutionAnomalies

    # Tendencias
    slippage_trend:      str   # stable | deteriorating | improving
    fill_trend:          str
    latency_trend:       str

    quality_recommendation: str
    audited_at:          str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["anomalies"] = asdict(self.anomalies)
        return d


# ── Auditor ────────────────────────────────────────────────────────────────────

class LiveExecutionAuditor:
    """
    Q-2: Audita qualidade de execucao comparando paper vs live.

    Le registros do log de execucao e calcula metricas de qualidade.
    Detecta deterioracao de exchange e fills inconsistentes.
    """

    def __init__(self, audit_log: Path = AUDIT_LOG):
        self.audit_log = audit_log

    def record_execution(self, record: ExecutionRecord) -> None:
        """Persiste um registro de execucao para auditoria futura."""
        try:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_log, "a") as f:
                f.write(json.dumps(record.to_dict()) + "\n")
        except Exception:
            pass

    def audit(self, window: int = 50) -> ExecutionAuditReport:
        """Audita as ultimas N execucoes e gera relatorio de qualidade."""
        records = self._load_records(window)

        live_records   = [r for r in records if r.get("mode") == "live"]
        paper_records  = [r for r in records if r.get("mode") == "paper"]

        if len(live_records) < MIN_SAMPLES_AUDIT:
            return self._minimal_report(len(live_records), len(paper_records))

        slippages  = [r["slippage_bps"] for r in live_records]
        fill_rates = [r["fill_rate"]    for r in live_records]
        latencies  = [r["latency_ms"]   for r in live_records]

        avg_slip  = statistics.mean(slippages)
        avg_fill  = statistics.mean(fill_rates)
        avg_lat   = statistics.mean(latencies)

        # Spread impact: estimado como metade do slippage medio
        avg_spread = avg_slip * 0.5

        # Execution drift: diferenca de slippage entre live e paper
        execution_drift = 0.0
        if paper_records:
            paper_slip = statistics.mean(r["slippage_bps"] for r in paper_records)
            execution_drift = avg_slip - paper_slip

        # Anomalias
        slippage_det = avg_slip > SLIPPAGE_WARN_BPS
        fill_incon   = avg_fill < FILL_QUALITY_WARN
        lat_spike    = avg_lat  > LATENCY_WARN_MS
        exchange_deg = sum([slippage_det, fill_incon, lat_spike]) >= 2

        anomalies = ExecutionAnomalies(
            slippage_deterioration = slippage_det,
            fill_inconsistency     = fill_incon,
            latency_spike          = lat_spike,
            exchange_degradation   = exchange_deg,
            anomaly_count          = sum([slippage_det, fill_incon, lat_spike]),
        )

        # Tendencias (primeira vs segunda metade)
        mid = len(live_records) // 2
        slippage_trend = self._trend(
            [r["slippage_bps"] for r in live_records[:mid]],
            [r["slippage_bps"] for r in live_records[mid:]],
        )
        fill_trend = self._trend(
            [r["fill_rate"] for r in live_records[:mid]],
            [r["fill_rate"] for r in live_records[mid:]],
            invert=True,
        )
        latency_trend = self._trend(
            [r["latency_ms"] for r in live_records[:mid]],
            [r["latency_ms"] for r in live_records[mid:]],
        )

        # Execution quality score
        quality = self._compute_quality(avg_slip, avg_fill, avg_lat, anomalies)

        recommendation = self._build_recommendation(
            quality, avg_slip, avg_fill, avg_lat, exchange_deg
        )

        report = ExecutionAuditReport(
            execution_quality_score = quality,
            avg_slippage_bps        = round(avg_slip, 2),
            avg_fill_quality        = round(avg_fill, 4),
            avg_latency_ms          = round(avg_lat, 1),
            avg_spread_impact       = round(avg_spread, 2),
            execution_drift         = round(execution_drift, 2),
            live_executions         = len(live_records),
            paper_executions        = len(paper_records),
            audit_window            = window,
            anomalies               = anomalies,
            slippage_trend          = slippage_trend,
            fill_trend              = fill_trend,
            latency_trend           = latency_trend,
            quality_recommendation  = recommendation,
            audited_at              = datetime.now(timezone.utc).isoformat(),
        )

        self._persist_summary(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_quality.set(quality)
                _prom_slippage.set(avg_slip)
                _prom_latency.set(avg_lat)
            except Exception:
                pass

        return report

    def _compute_quality(
        self, slip: float, fill: float, lat: float, anomalies: ExecutionAnomalies
    ) -> float:
        # Score base: 100 - penalidades
        score = 100.0
        score -= min(40.0, slip * 2.5)                     # slippage penalty
        score -= max(0.0, (1.0 - fill) * 50.0)            # fill penalty
        score -= min(20.0, max(0.0, (lat - 100) / 100.0) * 5.0)  # latency penalty
        score -= anomalies.anomaly_count * 5.0              # anomaly penalty
        if anomalies.exchange_degradation:
            score -= 15.0
        return max(0.0, min(100.0, round(score, 1)))

    def _trend(
        self, first_half: list[float], second_half: list[float], invert: bool = False
    ) -> str:
        if not first_half or not second_half:
            return "stable"
        avg_first  = statistics.mean(first_half)
        avg_second = statistics.mean(second_half)
        delta = avg_second - avg_first
        threshold = abs(avg_first) * 0.10  # 10% change = trend
        if abs(delta) < threshold:
            return "stable"
        if (delta > 0 and not invert) or (delta < 0 and invert):
            return "deteriorating"
        return "improving"

    def _build_recommendation(
        self, quality: float, slip: float, fill: float, lat: float, exchange_deg: bool
    ) -> str:
        if exchange_deg:
            return (
                f"EXCHANGE DEGRADATION: slippage={slip:.1f}bps fill={fill:.0%} lat={lat:.0f}ms. "
                "Considerar rollback para paper imediatamente."
            )
        if slip > SLIPPAGE_CRIT_BPS:
            return f"Slippage critico ({slip:.1f}bps). Usar apenas limit orders."
        if fill < FILL_QUALITY_CRIT:
            return f"Fill quality critica ({fill:.0%}). Reduzir tamanho de ordens."
        if quality >= 75:
            return f"Execucao de qualidade ({quality:.0f}/100). Continuar monitoramento."
        return f"Qualidade degradada ({quality:.0f}/100). Investigar exchange e timing."

    def _load_records(self, max_records: int) -> list[dict]:
        if not self.audit_log.exists():
            return []
        records: list[dict] = []
        try:
            with open(self.audit_log) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return records[-max_records:]

    def _persist_summary(self, report: ExecutionAuditReport) -> None:
        try:
            EXEC_AUDIT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "audited_at":              report.audited_at,
                "execution_quality_score": report.execution_quality_score,
                "avg_slippage_bps":        report.avg_slippage_bps,
                "avg_fill_quality":        report.avg_fill_quality,
                "avg_latency_ms":          report.avg_latency_ms,
                "exchange_degradation":    report.anomalies.exchange_degradation,
                "live_executions":         report.live_executions,
            }
            with open(EXEC_AUDIT_SUMMARY, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _minimal_report(self, live: int, paper: int) -> ExecutionAuditReport:
        return ExecutionAuditReport(
            execution_quality_score=75.0, avg_slippage_bps=0.0, avg_fill_quality=1.0,
            avg_latency_ms=0.0, avg_spread_impact=0.0, execution_drift=0.0,
            live_executions=live, paper_executions=paper, audit_window=0,
            anomalies=ExecutionAnomalies(False, False, False, False, 0),
            slippage_trend="stable", fill_trend="stable", latency_trend="stable",
            quality_recommendation=f"Apenas {live} execucoes live — dados insuficientes para auditoria.",
            audited_at=datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Execution Auditor — Phase Q Q-2")
    parser.add_argument("--record", action="store_true", help="Registrar execucao simulada")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    auditor = LiveExecutionAuditor()

    if args.record:
        rec = ExecutionRecord.build(
            mode="live", symbol="BTC/USDT", side="buy",
            expected_price=65000.0, executed_price=65008.5,
            requested_size=0.001, filled_size=0.001,
            latency_ms=145.0, fee_usd=0.065,
        )
        auditor.record_execution(rec)
        print(f"Registrado: {rec.record_id} slippage={rec.slippage_bps:.2f}bps fill={rec.fill_rate:.0%}")
        return

    report = auditor.audit()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nLive Execution Auditor")
    print(f"  execution_quality_score: {report.execution_quality_score:.0f}/100")
    print(f"  avg_slippage_bps:        {report.avg_slippage_bps:.2f}")
    print(f"  avg_fill_quality:        {report.avg_fill_quality:.0%}")
    print(f"  avg_latency_ms:          {report.avg_latency_ms:.0f}")
    print(f"  execution_drift:         {report.execution_drift:.2f}bps vs paper")
    print(f"  live_executions:         {report.live_executions}")
    print(f"\n  Tendencias:")
    print(f"    slippage: {report.slippage_trend}  fill: {report.fill_trend}  latency: {report.latency_trend}")
    print(f"\n  Anomalias:")
    a = report.anomalies
    print(f"    slippage_deterioration: {'SIM' if a.slippage_deterioration else 'nao'}")
    print(f"    fill_inconsistency:     {'SIM' if a.fill_inconsistency else 'nao'}")
    print(f"    latency_spike:          {'SIM' if a.latency_spike else 'nao'}")
    print(f"    exchange_degradation:   {'SIM' if a.exchange_degradation else 'nao'}")
    print(f"\n  -> {report.quality_recommendation}")


if __name__ == "__main__":
    main()

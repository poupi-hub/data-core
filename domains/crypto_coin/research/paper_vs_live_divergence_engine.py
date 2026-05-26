"""
paper_vs_live_divergence_engine.py — Phase Q Q-4

Paper vs Live Divergence Engine.

Compara resultados de execucao paper vs live e detecta divergencias
sistematicas que indicam problemas de qualidade de execucao.

Metricas calculadas:
  - divergence_score:          score geral de divergencia (0-100, 100 = pior)
  - live_consistency_score:    consistencia live vs paper (0-100, 100 = melhor)
  - execution_alignment_score: alinhamento de execucao (0-100, 100 = melhor)

Deteccoes:
  - divergence_escalation:     divergencia aumentando ao longo do tempo
  - live_inefficiency:         live sistematicamente pior que paper
  - slippage_gap:              gap de slippage live vs paper excessivo
  - fill_gap:                  gap de fill rate live vs paper excessivo
  - latency_gap:               gap de latencia live vs paper excessivo
  - execution_bias:            bias sistematico (live sempre pior em mesma direcao)

CLI:
  python -m domains.crypto_coin.research.paper_vs_live_divergence_engine
  python -m domains.crypto_coin.research.paper_vs_live_divergence_engine --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

DIVERGENCE_LOG  = Path("data/paper_vs_live_divergence_log.jsonl")
AUDIT_LOG       = Path("data/live_execution_audit_log.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import (
        divergence_score       as _prom_divergence,
        live_consistency_score as _prom_consistency,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Thresholds ─────────────────────────────────────────────────────────────────

SLIPPAGE_GAP_WARN_BPS   = 5.0    # gap de slippage live-paper > 5bps = warning
SLIPPAGE_GAP_CRIT_BPS   = 15.0   # gap de slippage live-paper > 15bps = critico
FILL_GAP_WARN           = 0.05   # live fill < paper fill - 5% = warning
FILL_GAP_CRIT           = 0.15   # live fill < paper fill - 15% = critico
LATENCY_GAP_WARN_MS     = 100    # live latencia > paper latencia + 100ms = warning
LATENCY_GAP_CRIT_MS     = 500    # live latencia > paper latencia + 500ms = critico
MIN_SAMPLES_EACH        = 3      # minimo por modo para comparacao valida
DIVERGENCE_WARN         = 30.0   # divergence_score > 30 = warning
DIVERGENCE_CRIT         = 60.0   # divergence_score > 60 = critico


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class DivergenceDetections:
    divergence_escalation: bool
    live_inefficiency:     bool
    slippage_gap:          bool
    fill_gap:              bool
    latency_gap:           bool
    execution_bias:        bool
    detection_count:       int


@dataclass
class DivergenceReport:
    """Relatorio de divergencia paper vs live."""
    report_id:               str

    # Scores principais
    divergence_score:        float   # 0-100 (100 = maxima divergencia)
    live_consistency_score:  float   # 0-100 (100 = totalmente consistente)
    execution_alignment_score: float # 0-100 (100 = alinhado com paper)

    # Metricas paper
    paper_avg_slippage_bps:  float
    paper_avg_fill_rate:     float
    paper_avg_latency_ms:    float

    # Metricas live
    live_avg_slippage_bps:   float
    live_avg_fill_rate:      float
    live_avg_latency_ms:     float

    # Gaps
    slippage_gap_bps:        float   # live - paper
    fill_rate_gap:           float   # paper - live (positivo = live pior)
    latency_gap_ms:          float   # live - paper

    # Samples
    paper_executions:        int
    live_executions:         int
    analysis_window:         int

    # Deteccoes
    detections:              DivergenceDetections

    # Trends
    divergence_trend:        str    # stable | escalating | improving
    severity:                str    # low | medium | high | critical

    recommendation:          str
    evaluated_at:            str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detections"] = asdict(self.detections)
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class PaperVsLiveDivergenceEngine:
    """
    Q-4: Compara resultados paper vs live para detectar divergencias sistematicas.

    Lê do AUDIT_LOG que contem registros de ambos os modos.
    """

    def __init__(
        self,
        divergence_log: Path = DIVERGENCE_LOG,
        audit_log:      Path = AUDIT_LOG,
        window:         int  = 50,
    ):
        self.divergence_log = divergence_log
        self.audit_log      = audit_log
        self.window         = window

    def evaluate(self) -> DivergenceReport:
        """Calcula divergencia paper vs live e retorna relatorio detalhado."""
        report_id = str(uuid.uuid4())[:10]
        records   = self._load_records()

        paper_recs = [r for r in records if r.get("mode") == "paper"]
        live_recs  = [r for r in records if r.get("mode") == "live"]

        if len(paper_recs) < MIN_SAMPLES_EACH or len(live_recs) < MIN_SAMPLES_EACH:
            return self._minimal_report(report_id, len(paper_recs), len(live_recs))

        # ── Paper metrics ──────────────────────────────────────────────────────
        paper_slip = statistics.mean(r["slippage_bps"] for r in paper_recs)
        paper_fill = statistics.mean(r["fill_rate"]    for r in paper_recs)
        paper_lat  = statistics.mean(r["latency_ms"]   for r in paper_recs)

        # ── Live metrics ───────────────────────────────────────────────────────
        live_slip  = statistics.mean(r["slippage_bps"] for r in live_recs)
        live_fill  = statistics.mean(r["fill_rate"]    for r in live_recs)
        live_lat   = statistics.mean(r["latency_ms"]   for r in live_recs)

        # ── Gaps ───────────────────────────────────────────────────────────────
        slip_gap  = live_slip - paper_slip     # positivo = live pior
        fill_gap  = paper_fill - live_fill     # positivo = live pior
        lat_gap   = live_lat - paper_lat       # positivo = live pior

        # ── Deteccoes ──────────────────────────────────────────────────────────
        det_slip    = slip_gap > SLIPPAGE_GAP_WARN_BPS
        det_fill    = fill_gap > FILL_GAP_WARN
        det_lat     = lat_gap  > LATENCY_GAP_WARN_MS
        det_ineff   = (slip_gap > SLIPPAGE_GAP_WARN_BPS and
                       fill_gap > FILL_GAP_WARN and
                       lat_gap  > LATENCY_GAP_WARN_MS)
        det_bias    = self._detect_execution_bias(live_recs)
        det_escalat = self._detect_divergence_escalation(live_recs, paper_recs)
        det_count   = sum([det_slip, det_fill, det_lat, det_ineff, det_bias, det_escalat])

        detections = DivergenceDetections(
            divergence_escalation = det_escalat,
            live_inefficiency     = det_ineff,
            slippage_gap          = det_slip,
            fill_gap              = det_fill,
            latency_gap           = det_lat,
            execution_bias        = det_bias,
            detection_count       = det_count,
        )

        # ── Scores ────────────────────────────────────────────────────────────
        divergence_score = self._compute_divergence_score(
            slip_gap, fill_gap, lat_gap, det_count
        )
        consistency_score  = max(0.0, 100.0 - divergence_score)
        alignment_score    = self._compute_alignment_score(slip_gap, fill_gap, lat_gap)

        divergence_trend = self._compute_trend(live_recs)
        severity = (
            "critical" if divergence_score >= DIVERGENCE_CRIT else
            "high"     if divergence_score >= DIVERGENCE_WARN * 1.5 else
            "medium"   if divergence_score >= DIVERGENCE_WARN else
            "low"
        )

        recommendation = self._build_recommendation(
            divergence_score, slip_gap, fill_gap, lat_gap, severity, det_ineff
        )

        report = DivergenceReport(
            report_id                = report_id,
            divergence_score         = round(divergence_score, 1),
            live_consistency_score   = round(consistency_score, 1),
            execution_alignment_score = round(alignment_score, 1),
            paper_avg_slippage_bps   = round(paper_slip, 2),
            paper_avg_fill_rate      = round(paper_fill, 4),
            paper_avg_latency_ms     = round(paper_lat, 1),
            live_avg_slippage_bps    = round(live_slip, 2),
            live_avg_fill_rate       = round(live_fill, 4),
            live_avg_latency_ms      = round(live_lat, 1),
            slippage_gap_bps         = round(slip_gap, 2),
            fill_rate_gap            = round(fill_gap, 4),
            latency_gap_ms           = round(lat_gap, 1),
            paper_executions         = len(paper_recs),
            live_executions          = len(live_recs),
            analysis_window          = self.window,
            detections               = detections,
            divergence_trend         = divergence_trend,
            severity                 = severity,
            recommendation           = recommendation,
            evaluated_at             = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_divergence.set(divergence_score)
                _prom_consistency.set(consistency_score)
            except Exception:
                pass

        return report

    # ── Computation ────────────────────────────────────────────────────────────

    def _compute_divergence_score(
        self, slip_gap: float, fill_gap: float, lat_gap: float, det_count: int
    ) -> float:
        score = 0.0
        # Slippage gap (max 40 pontos)
        score += min(40.0, max(0.0, slip_gap / SLIPPAGE_GAP_CRIT_BPS * 40.0))
        # Fill gap (max 30 pontos)
        score += min(30.0, max(0.0, fill_gap / FILL_GAP_CRIT * 30.0))
        # Latency gap (max 20 pontos)
        score += min(20.0, max(0.0, lat_gap / LATENCY_GAP_CRIT_MS * 20.0))
        # Detection count (max 10 pontos)
        score += min(10.0, det_count * 2.0)
        return max(0.0, min(100.0, round(score, 1)))

    def _compute_alignment_score(
        self, slip_gap: float, fill_gap: float, lat_gap: float
    ) -> float:
        slip_pen = min(40.0, max(0.0, slip_gap / SLIPPAGE_GAP_CRIT_BPS * 40.0))
        fill_pen = min(30.0, max(0.0, fill_gap / FILL_GAP_CRIT * 30.0))
        lat_pen  = min(20.0, max(0.0, lat_gap  / LATENCY_GAP_CRIT_MS * 20.0))
        return max(0.0, min(100.0, 100.0 - slip_pen - fill_pen - lat_pen))

    def _detect_execution_bias(self, live_recs: list[dict]) -> bool:
        """Detecta se live e sistematicamente pior em slippage (bias)."""
        if len(live_recs) < 6:
            return False
        slippages = [r.get("slippage_bps", 0.0) for r in live_recs]
        mid = len(slippages) // 2
        first_avg = statistics.mean(slippages[:mid])
        last_avg  = statistics.mean(slippages[mid:])
        # Bias = segunda metade pior que primeira E acima do threshold
        return last_avg > first_avg * 1.2 and last_avg > SLIPPAGE_GAP_WARN_BPS

    def _detect_divergence_escalation(
        self, live_recs: list[dict], paper_recs: list[dict]
    ) -> bool:
        """Verifica se divergencia de slippage esta escalando."""
        if len(live_recs) < 4:
            return False
        mid = len(live_recs) // 2
        paper_slip = statistics.mean(r.get("slippage_bps", 0.0) for r in paper_recs)
        gap_first  = statistics.mean(r.get("slippage_bps", 0.0) for r in live_recs[:mid]) - paper_slip
        gap_last   = statistics.mean(r.get("slippage_bps", 0.0) for r in live_recs[mid:]) - paper_slip
        return gap_last > gap_first * 1.3 and gap_last > SLIPPAGE_GAP_WARN_BPS

    def _compute_trend(self, live_recs: list[dict]) -> str:
        if len(live_recs) < 4:
            return "stable"
        mid = len(live_recs) // 2
        first = statistics.mean(r.get("slippage_bps", 0.0) for r in live_recs[:mid])
        last  = statistics.mean(r.get("slippage_bps", 0.0) for r in live_recs[mid:])
        delta = last - first
        threshold = abs(first) * 0.10
        if abs(delta) < threshold:
            return "stable"
        return "escalating" if delta > 0 else "improving"

    def _build_recommendation(
        self, score: float, slip_gap: float, fill_gap: float,
        lat_gap: float, severity: str, inefficiency: bool,
    ) -> str:
        if severity == "critical" or inefficiency:
            return (
                f"DIVERGENCIA CRITICA: slip_gap={slip_gap:.1f}bps fill_gap={fill_gap:.0%} "
                f"lat_gap={lat_gap:.0f}ms. Live consistentemente pior que paper. "
                "Considerar rollback para paper."
            )
        if severity == "high":
            return (
                f"Divergencia alta (score={score:.0f}): slip_gap={slip_gap:.1f}bps. "
                "Investigar microestrutura e timing de ordens."
            )
        if severity == "medium":
            return f"Divergencia moderada (score={score:.0f}). Monitorar tendencia."
        return f"Divergencia baixa (score={score:.0f}). Execucao alinhada com paper."

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: DivergenceReport) -> None:
        try:
            self.divergence_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":          report.evaluated_at,
                "divergence_score":      report.divergence_score,
                "live_consistency_score": report.live_consistency_score,
                "slippage_gap_bps":      report.slippage_gap_bps,
                "fill_rate_gap":         report.fill_rate_gap,
                "latency_gap_ms":        report.latency_gap_ms,
                "severity":              report.severity,
                "detection_count":       report.detections.detection_count,
            }
            with open(self.divergence_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _load_records(self) -> list[dict]:
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
        return records[-self.window:]

    def _minimal_report(self, report_id: str, n_paper: int, n_live: int) -> DivergenceReport:
        msg = (
            f"Insuficiente: paper={n_paper} live={n_live} "
            f"(minimo {MIN_SAMPLES_EACH} cada)."
        )
        empty_det = DivergenceDetections(False,False,False,False,False,False,0)
        return DivergenceReport(
            report_id=report_id, divergence_score=0.0,
            live_consistency_score=100.0, execution_alignment_score=100.0,
            paper_avg_slippage_bps=0.0, paper_avg_fill_rate=1.0, paper_avg_latency_ms=0.0,
            live_avg_slippage_bps=0.0, live_avg_fill_rate=1.0, live_avg_latency_ms=0.0,
            slippage_gap_bps=0.0, fill_rate_gap=0.0, latency_gap_ms=0.0,
            paper_executions=n_paper, live_executions=n_live, analysis_window=self.window,
            detections=empty_det, divergence_trend="stable", severity="low",
            recommendation=msg, evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper vs Live Divergence Engine — Phase Q Q-4")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = PaperVsLiveDivergenceEngine()
    report = engine.evaluate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nPaper vs Live Divergence Engine")
    print(f"  divergence_score:          {report.divergence_score:.1f}/100")
    print(f"  live_consistency_score:    {report.live_consistency_score:.1f}/100")
    print(f"  execution_alignment_score: {report.execution_alignment_score:.1f}/100")
    print(f"  severity:                  {report.severity}")
    print(f"  divergence_trend:          {report.divergence_trend}")
    print(f"\n  Paper  (n={report.paper_executions}): slip={report.paper_avg_slippage_bps:.2f}bps fill={report.paper_avg_fill_rate:.0%} lat={report.paper_avg_latency_ms:.0f}ms")
    print(f"  Live   (n={report.live_executions}): slip={report.live_avg_slippage_bps:.2f}bps fill={report.live_avg_fill_rate:.0%} lat={report.live_avg_latency_ms:.0f}ms")
    print(f"\n  Gaps:")
    print(f"    slippage_gap:  {report.slippage_gap_bps:+.2f}bps")
    print(f"    fill_gap:      {report.fill_rate_gap:+.2%}")
    print(f"    latency_gap:   {report.latency_gap_ms:+.0f}ms")
    print(f"\n  Deteccoes ({report.detections.detection_count} ativas):")
    d = report.detections
    print(f"    divergence_escalation: {'SIM' if d.divergence_escalation else 'nao'}")
    print(f"    live_inefficiency:     {'SIM' if d.live_inefficiency else 'nao'}")
    print(f"    slippage_gap:          {'SIM' if d.slippage_gap else 'nao'}")
    print(f"    fill_gap:              {'SIM' if d.fill_gap else 'nao'}")
    print(f"    latency_gap:           {'SIM' if d.latency_gap else 'nao'}")
    print(f"    execution_bias:        {'SIM' if d.execution_bias else 'nao'}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

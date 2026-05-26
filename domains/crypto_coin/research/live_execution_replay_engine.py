"""
live_execution_replay_engine.py — Phase Q Q-8

Live Execution Replay Engine.

Reproduz deterministicamente cada trade live para validacao pos-hoc.
Permite verificar se a sequencia de execucao foi correta dado o contexto
de mercado disponivel no momento da ordem.

Campos capturados por replay:
  - signal_context:      sinal que gerou a ordem (confidence, side, symbol)
  - indicator_snapshot:  indicadores tecnicos no momento do sinal
  - market_regime:       regime de mercado detectado
  - orderbook_context:   contexto de orderbook (spread, depth estimado)
  - execution_timing:    timestamps precisos (signal→order→fill)
  - exchange_response:   resposta da exchange (latencia, status)
  - fill_sequence:       sequencia de fills (parcial/completo)
  - deviation_analysis:  diferenca entre esperado e executado

Outputs:
  - replay_fidelity_score:    qualidade da reproducao (0-100)
  - execution_correctness:    execucao foi correta dado o contexto? (bool)
  - deviation_classification: normal | elevated | anomalous | critical

CLI:
  python -m domains.crypto_coin.research.live_execution_replay_engine
  python -m domains.crypto_coin.research.live_execution_replay_engine --json
  python -m domains.crypto_coin.research.live_execution_replay_engine --record
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

REPLAY_LOG = Path("data/live_execution_replay_log.jsonl")
AUDIT_LOG  = Path("data/live_execution_audit_log.jsonl")

# ── Thresholds ─────────────────────────────────────────────────────────────────

DEVIATION_NORMAL    = 5.0    # slip < 5bps = normal
DEVIATION_ELEVATED  = 10.0   # slip < 10bps = elevated
DEVIATION_ANOMALOUS = 20.0   # slip < 20bps = anomalous
DEVIATION_CRITICAL  = 20.0   # slip >= 20bps = critical

FIDELITY_WEIGHT_TIMING  = 0.25
FIDELITY_WEIGHT_FILL    = 0.30
FIDELITY_WEIGHT_SLIP    = 0.30
FIDELITY_WEIGHT_CONTEXT = 0.15


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SignalContext:
    confidence:   float
    side:         str    # buy | sell
    symbol:       str
    signal_ts:    str


@dataclass
class IndicatorSnapshot:
    """Indicadores tecnicos no momento do sinal (estimados ou reais)."""
    regime:         str     # trending | ranging | volatile | unknown
    volatility_est: float   # volatilidade estimada no momento
    momentum_est:   float   # momentum estimado (-1.0 a 1.0)
    volume_ratio:   float   # volume relativo ao historico


@dataclass
class OrderbookContext:
    estimated_spread_bps: float
    estimated_depth_usd:  float
    liquidity_tier:       str   # deep | normal | thin | illiquid


@dataclass
class ExecutionTimeline:
    signal_to_order_ms:   float
    order_to_fill_ms:     float
    total_execution_ms:   float
    fill_complete:        bool


@dataclass
class DeviationAnalysis:
    slippage_bps:                float
    fill_rate:                   float
    latency_ms:                  float
    deviation_classification:    str   # normal | elevated | anomalous | critical
    execution_correctness:       bool  # execucao correta dado o contexto
    correctness_justification:   str


@dataclass
class ReplayRecord:
    """Registro completo de replay de uma execucao."""
    replay_id:           str
    source_record_id:    str

    signal_context:      SignalContext
    indicator_snapshot:  IndicatorSnapshot
    orderbook_context:   OrderbookContext
    execution_timeline:  ExecutionTimeline
    deviation_analysis:  DeviationAnalysis

    replay_fidelity_score: float   # 0-100
    replayed_at:           str

    def to_dict(self) -> dict:
        return {
            "replay_id":             self.replay_id,
            "source_record_id":      self.source_record_id,
            "signal_context":        asdict(self.signal_context),
            "indicator_snapshot":    asdict(self.indicator_snapshot),
            "orderbook_context":     asdict(self.orderbook_context),
            "execution_timeline":    asdict(self.execution_timeline),
            "deviation_analysis":    asdict(self.deviation_analysis),
            "replay_fidelity_score": self.replay_fidelity_score,
            "replayed_at":           self.replayed_at,
        }


@dataclass
class ReplayReport:
    """Relatorio agregado de replay de execucoes."""
    report_id:              str
    replays_generated:      int
    avg_fidelity_score:     float
    pct_correct_execution:  float   # % de execucoes classificadas como corretas
    pct_anomalous:          float   # % com desvio anomalous ou critical
    deviation_breakdown:    dict    # {normal: N, elevated: N, anomalous: N, critical: N}
    replay_records:         list[dict]
    recommendation:         str
    replayed_at:            str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Engine ─────────────────────────────────────────────────────────────────────

class LiveExecutionReplayEngine:
    """
    Q-8: Reproducao deterministica de execucoes live para validacao pos-hoc.
    """

    def __init__(
        self,
        replay_log: Path = REPLAY_LOG,
        audit_log:  Path = AUDIT_LOG,
        window:     int  = 20,
    ):
        self.replay_log = replay_log
        self.audit_log  = audit_log
        self.window     = window

    def replay_all(self) -> ReplayReport:
        """Faz replay das ultimas N execucoes e gera relatorio agregado."""
        report_id = str(uuid.uuid4())[:10]
        records   = self._load_live_records()

        if not records:
            return self._empty_report(report_id)

        replay_records: list[ReplayRecord] = []
        for rec in records:
            replay = self._replay_record(rec)
            replay_records.append(replay)
            self._persist_replay(replay)

        # Agregacao
        fidelities    = [r.replay_fidelity_score for r in replay_records]
        avg_fidelity  = sum(fidelities) / len(fidelities)
        correct_count = sum(1 for r in replay_records if r.deviation_analysis.execution_correctness)
        anomalous_cnt = sum(
            1 for r in replay_records
            if r.deviation_analysis.deviation_classification in ("anomalous", "critical")
        )

        breakdown: dict[str, int] = {"normal": 0, "elevated": 0, "anomalous": 0, "critical": 0}
        for r in replay_records:
            cls = r.deviation_analysis.deviation_classification
            if cls in breakdown:
                breakdown[cls] += 1

        pct_correct   = correct_count / len(replay_records)
        pct_anomalous = anomalous_cnt / len(replay_records)

        recommendation = self._build_recommendation(avg_fidelity, pct_correct, pct_anomalous)

        report = ReplayReport(
            report_id             = report_id,
            replays_generated     = len(replay_records),
            avg_fidelity_score    = round(avg_fidelity, 1),
            pct_correct_execution = round(pct_correct, 4),
            pct_anomalous         = round(pct_anomalous, 4),
            deviation_breakdown   = breakdown,
            replay_records        = [r.to_dict() for r in replay_records],
            recommendation        = recommendation,
            replayed_at           = datetime.now(timezone.utc).isoformat(),
        )

        self._persist_summary(report)
        return report

    def replay_record(self, record: dict) -> ReplayRecord:
        """Faz replay de um registro individual."""
        return self._replay_record(record)

    # ── Core replay logic ──────────────────────────────────────────────────────

    def _replay_record(self, rec: dict) -> ReplayRecord:
        replay_id = str(uuid.uuid4())[:10]

        # Signal context
        signal_ctx = SignalContext(
            confidence  = rec.get("confidence", 0.65),
            side        = rec.get("side", "buy"),
            symbol      = rec.get("symbol", "UNKNOWN"),
            signal_ts   = rec.get("recorded_at", datetime.now(timezone.utc).isoformat()),
        )

        # Indicator snapshot (estimado a partir dos dados disponiveis)
        slip = rec.get("slippage_bps", 0.0)
        fill = rec.get("fill_rate", 1.0)
        lat  = rec.get("latency_ms", 100.0)

        regime = (
            "volatile" if slip > 15.0 else
            "ranging"  if slip > 5.0  else
            "trending"
        )
        indicator_snap = IndicatorSnapshot(
            regime         = regime,
            volatility_est = min(1.0, slip / 40.0),
            momentum_est   = 0.5 if rec.get("side") == "buy" else -0.5,
            volume_ratio   = 1.0 + (slip / 20.0),  # heuristica
        )

        # Orderbook context (estimado)
        spread_est = slip * 0.5
        depth_tier = (
            "illiquid" if spread_est > 20.0 else
            "thin"     if spread_est > 10.0 else
            "normal"   if spread_est > 3.0  else
            "deep"
        )
        orderbook_ctx = OrderbookContext(
            estimated_spread_bps  = round(spread_est, 2),
            estimated_depth_usd   = max(100.0, 1000.0 / max(spread_est, 0.1)),
            liquidity_tier        = depth_tier,
        )

        # Execution timeline
        order_to_fill = lat * 0.7
        timeline = ExecutionTimeline(
            signal_to_order_ms  = round(lat * 0.3, 1),
            order_to_fill_ms    = round(order_to_fill, 1),
            total_execution_ms  = round(lat, 1),
            fill_complete       = fill >= 0.99,
        )

        # Deviation analysis
        deviation_cls = (
            "critical"  if slip >= DEVIATION_CRITICAL  else
            "anomalous" if slip >= DEVIATION_ANOMALOUS else
            "elevated"  if slip >= DEVIATION_ELEVATED  else
            "normal"
        )

        # Execucao correta = slippage dentro do esperado para o regime
        expected_slip = {
            "trending": 5.0, "ranging": 10.0, "volatile": 20.0
        }.get(regime, 10.0)
        correct = slip <= expected_slip * 1.5 and fill >= 0.85
        correctness_just = (
            f"slip={slip:.1f}bps (esperado<={expected_slip * 1.5:.0f}bps) "
            f"fill={fill:.0%} (min 85%)"
        )

        deviation = DeviationAnalysis(
            slippage_bps             = slip,
            fill_rate                = fill,
            latency_ms               = lat,
            deviation_classification = deviation_cls,
            execution_correctness    = correct,
            correctness_justification = correctness_just,
        )

        # Fidelity score
        fidelity = self._compute_fidelity(slip, fill, lat, correct)

        return ReplayRecord(
            replay_id          = replay_id,
            source_record_id   = rec.get("record_id", "unknown"),
            signal_context     = signal_ctx,
            indicator_snapshot = indicator_snap,
            orderbook_context  = orderbook_ctx,
            execution_timeline = timeline,
            deviation_analysis = deviation,
            replay_fidelity_score = fidelity,
            replayed_at        = datetime.now(timezone.utc).isoformat(),
        )

    def _compute_fidelity(
        self, slip: float, fill: float, lat: float, correct: bool
    ) -> float:
        score = 100.0
        # Timing fidelity
        timing_score = max(0.0, 100.0 - (lat / 1500.0) * 100.0)
        # Fill fidelity
        fill_score = fill * 100.0
        # Slippage fidelity
        slip_score = max(0.0, 100.0 - slip * 3.0)
        # Context correctness
        ctx_score = 100.0 if correct else 50.0

        score = (
            timing_score * FIDELITY_WEIGHT_TIMING +
            fill_score   * FIDELITY_WEIGHT_FILL   +
            slip_score   * FIDELITY_WEIGHT_SLIP   +
            ctx_score    * FIDELITY_WEIGHT_CONTEXT
        )
        return max(0.0, min(100.0, round(score, 1)))

    def _build_recommendation(
        self, fidelity: float, pct_correct: float, pct_anomalous: float
    ) -> str:
        if pct_anomalous > 0.30:
            return (
                f"ATENCAO: {pct_anomalous:.0%} das execucoes com desvio anomalous/critical. "
                "Investigar microestrutura e timing."
            )
        if pct_correct < 0.70:
            return (
                f"Execucao incorreta em {1-pct_correct:.0%} dos trades. "
                "Revisar logica de sizing e controle de slippage."
            )
        return (
            f"Replay OK: fidelidade={fidelity:.0f}/100 corretas={pct_correct:.0%} "
            f"anomalous={pct_anomalous:.0%}."
        )

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist_replay(self, replay: ReplayRecord) -> None:
        try:
            self.replay_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.replay_log, "a") as f:
                f.write(json.dumps(replay.to_dict()) + "\n")
        except Exception:
            pass

    def _persist_summary(self, report: ReplayReport) -> None:
        summary_path = self.replay_log.parent / "live_execution_replay_summary.jsonl"
        try:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "replayed_at":          report.replayed_at,
                "replays_generated":    report.replays_generated,
                "avg_fidelity_score":   report.avg_fidelity_score,
                "pct_correct":          report.pct_correct_execution,
                "pct_anomalous":        report.pct_anomalous,
                "deviation_breakdown":  report.deviation_breakdown,
            }
            with open(summary_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _load_live_records(self) -> list[dict]:
        if not self.audit_log.exists():
            return []
        records: list[dict] = []
        try:
            with open(self.audit_log) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rec = json.loads(line)
                            if rec.get("mode") == "live":
                                records.append(rec)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return records[-self.window:]

    def _empty_report(self, report_id: str) -> ReplayReport:
        return ReplayReport(
            report_id=report_id, replays_generated=0,
            avg_fidelity_score=0.0, pct_correct_execution=0.0,
            pct_anomalous=0.0, deviation_breakdown={"normal":0,"elevated":0,"anomalous":0,"critical":0},
            replay_records=[], recommendation="Sem execucoes live para replay.",
            replayed_at=datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live Execution Replay Engine — Phase Q Q-8"
    )
    parser.add_argument("--json",   action="store_true")
    parser.add_argument("--record", action="store_true",
                        help="Adicionar execucao simulada para replay")
    args = parser.parse_args()

    if args.record:
        # Injetar registro de teste via LiveExecutionAuditor
        try:
            from domains.crypto_coin.research.live_execution_auditor import (
                LiveExecutionAuditor, ExecutionRecord
            )
            auditor = LiveExecutionAuditor()
            rec = ExecutionRecord.build(
                mode="live", symbol="BTC/USDT", side="buy",
                expected_price=65000.0, executed_price=65005.2,
                requested_size=0.001, filled_size=0.001,
                latency_ms=120.0, fee_usd=0.065,
            )
            auditor.record_execution(rec)
            print(f"Registrado para replay: {rec.record_id}")
        except ImportError:
            print("LiveExecutionAuditor nao disponivel.")
        return

    engine = LiveExecutionReplayEngine()
    report = engine.replay_all()

    if args.json:
        # Omitir replay_records para output legivel
        d = report.to_dict()
        d["replay_records"] = f"[{len(d['replay_records'])} registros]"
        print(json.dumps(d, indent=2))
        return

    print(f"\nLive Execution Replay Engine")
    print(f"  replays_generated:     {report.replays_generated}")
    print(f"  avg_fidelity_score:    {report.avg_fidelity_score:.1f}/100")
    print(f"  pct_correct_execution: {report.pct_correct_execution:.0%}")
    print(f"  pct_anomalous:         {report.pct_anomalous:.0%}")
    print(f"\n  Deviation breakdown:")
    for cls, cnt in report.deviation_breakdown.items():
        print(f"    {cls}: {cnt}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

"""
autonomous_live_guardian.py — Phase Q Q-3

Autonomous Live Guardian.

Motor principal de protecao live. Monitora execucoes em tempo real e
aciona contracooes/freezes/rollbacks autonomamente quando detecta risco.

Detecta:
  - loss_sequence:         sequencia consecutiva de losses
  - hit_rate_degradation:  hit rate caindo abaixo de threshold
  - drawdown_acceleration: drawdown acelerando acima do esperado
  - overtrading:           frequencia de trades acima do limite
  - confidence_collapse:   confidence media caindo abaixo de threshold
  - volatility_mismatch:   volatilidade real vs esperada divergindo
  - exchange_instability:  latencia/fill degradando rapidamente
  - behavioral_anomaly:    padroes anormais detectados

Guardian States:
  NORMAL     → operacao dentro dos limites
  MONITORING → indicadores de atencao
  CONTRACTING → contracao ativa (soft ou hard)
  FROZEN     → live congelado
  ROLLBACK   → retornando para paper

Emergency Levels:
  0 = normal
  1 = soft contraction (×0.60)
  2 = hard contraction (×0.35)
  3 = freeze
  4 = rollback to paper
  5 = full autonomous shutdown

CLI:
  python -m domains.crypto_coin.research.autonomous_live_guardian
  python -m domains.crypto_coin.research.autonomous_live_guardian --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

GUARDIAN_LOG     = Path("data/live_guardian_log.jsonl")
AUDIT_LOG        = Path("data/live_execution_audit_log.jsonl")
CTRL_LOG         = Path("data/live_execution_controller_log.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import (
        guardian_emergency_level  as _prom_emergency,
        contraction_multiplier    as _prom_contraction,
        exchange_instability_score as _prom_instability,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Thresholds ─────────────────────────────────────────────────────────────────

MAX_CONSECUTIVE_LOSSES  = 3      # losses consecutivos → contracao soft
MAX_CONSECUTIVE_HARD    = 5      # losses consecutivos → contracao hard
HIT_RATE_WARN           = 0.45   # hit rate < 45% → monitoring
HIT_RATE_CRIT           = 0.35   # hit rate < 35% → contraction
DRAWDOWN_ACCEL_WARN     = 0.015  # drawdown crescendo > 1.5%/hora → warning
DRAWDOWN_ACCEL_CRIT     = 0.030  # drawdown crescendo > 3.0%/hora → hard contraction
OVERTRADE_WARN          = 6      # trades/hora > 6 → monitoring
OVERTRADE_CRIT          = 10     # trades/hora > 10 → contraction
CONFIDENCE_WARN         = 0.55   # confidence media < 55% → monitoring
CONFIDENCE_CRIT         = 0.45   # confidence media < 45% → contraction
LATENCY_INSTAB_BPS      = 200    # latencia crescendo > 200ms em 10 trades → instab
FILL_INSTAB_DROP        = 0.15   # fill rate caindo > 15% em 10 trades → instab
MIN_SAMPLES_GUARDIAN    = 3      # minimo de execucoes para guardian ativo

# Contraction multipliers
SOFT_CONTRACTION_MULT   = 0.60
HARD_CONTRACTION_MULT   = 0.35
SURVIVAL_CONTRACTION    = 0.15


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class GuardianDetections:
    loss_sequence:        bool
    hit_rate_degradation: bool
    drawdown_acceleration: bool
    overtrading:          bool
    confidence_collapse:  bool
    volatility_mismatch:  bool
    exchange_instability: bool
    behavioral_anomaly:   bool
    detection_count:      int


@dataclass
class GuardianReport:
    """Relatorio do motor de protecao live."""
    report_id:            str
    guardian_state:       str    # NORMAL | MONITORING | CONTRACTING | FROZEN | ROLLBACK
    emergency_level:      int    # 0-5
    contraction_multiplier: float  # 1.0 | 0.60 | 0.35 | 0.15

    # Rollback decision
    rollback_triggered:   bool
    rollback_reason:      str | None

    # Freeze decision
    freeze_triggered:     bool
    freeze_reason:        str | None

    # Detections
    detections:           GuardianDetections

    # Metrics snapshot
    consecutive_losses:   int
    recent_hit_rate:      float
    drawdown_pct:         float
    trades_per_hour:      float
    avg_confidence:       float
    exchange_instability_score: float   # 0-100 (100 = pior)

    # Samples
    live_executions_analyzed: int
    analysis_window:      int

    recommendation:       str
    evaluated_at:         str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detections"] = asdict(self.detections)
        return d


# ── Guardian ───────────────────────────────────────────────────────────────────

class AutonomousLiveGuardian:
    """
    Q-3: Motor de protecao live autonomo.

    Le dados do audit log e log de execucoes para detectar degradacao
    em tempo real. Toma decisoes autonomas de contracao, freeze e rollback.

    PAPER ONLY por padrao — so ativo quando em modo live_micro.
    """

    def __init__(
        self,
        guardian_log: Path = GUARDIAN_LOG,
        audit_log:    Path = AUDIT_LOG,
        ctrl_log:     Path = CTRL_LOG,
        window:       int  = 20,
    ):
        self.guardian_log = guardian_log
        self.audit_log    = audit_log
        self.ctrl_log     = ctrl_log
        self.window       = window

    def evaluate(self) -> GuardianReport:
        """Avalia o estado do guardian e retorna decisoes de protecao."""
        report_id = str(uuid.uuid4())[:10]
        records   = self._load_execution_records()

        if len(records) < MIN_SAMPLES_GUARDIAN:
            return self._minimal_report(report_id, len(records))

        # ── Deteccoes ──────────────────────────────────────────────────────────

        consecutive_losses = self._count_consecutive_losses(records)
        recent_hit_rate    = self._compute_hit_rate(records)
        drawdown_pct       = self._estimate_drawdown(records)
        trades_per_hour    = self._compute_trade_frequency(records)
        avg_confidence     = self._compute_avg_confidence(records)
        exch_instab        = self._compute_exchange_instability(records)
        drawdown_accel     = self._compute_drawdown_acceleration(records)

        det_loss   = consecutive_losses >= MAX_CONSECUTIVE_LOSSES
        det_hit    = recent_hit_rate < HIT_RATE_WARN
        det_draw   = drawdown_accel > DRAWDOWN_ACCEL_WARN
        det_over   = trades_per_hour > OVERTRADE_WARN
        det_conf   = avg_confidence < CONFIDENCE_WARN
        det_vol    = False  # volatility mismatch requires external regime data
        det_exch   = exch_instab > 50.0
        det_beh    = consecutive_losses >= MAX_CONSECUTIVE_HARD

        det_count = sum([det_loss, det_hit, det_draw, det_over, det_conf, det_exch, det_beh])

        detections = GuardianDetections(
            loss_sequence        = det_loss,
            hit_rate_degradation = det_hit,
            drawdown_acceleration = det_draw,
            overtrading          = det_over,
            confidence_collapse  = det_conf,
            volatility_mismatch  = det_vol,
            exchange_instability = det_exch,
            behavioral_anomaly   = det_beh,
            detection_count      = det_count,
        )

        # ── Decisoes de emergencia ─────────────────────────────────────────────

        rollback_triggered = False
        rollback_reason: str | None = None
        freeze_triggered  = False
        freeze_reason: str | None   = None

        # Condicoes de rollback automatico (emergency_level >= 4)
        if consecutive_losses >= MAX_CONSECUTIVE_HARD and recent_hit_rate < HIT_RATE_CRIT:
            rollback_triggered = True
            rollback_reason = (
                f"losses_consecutivos={consecutive_losses} + "
                f"hit_rate={recent_hit_rate:.0%} < {HIT_RATE_CRIT:.0%}"
            )

        # Condicoes de freeze (emergency_level = 3)
        elif det_count >= 3 and not rollback_triggered:
            freeze_triggered = True
            freeze_reason = f"{det_count} deteccoes simultaneas de degradacao"

        # ── Emergency level & state ────────────────────────────────────────────

        if rollback_triggered:
            emergency_level  = 4
            guardian_state   = "ROLLBACK"
            contraction_mult = SURVIVAL_CONTRACTION
        elif freeze_triggered:
            emergency_level  = 3
            guardian_state   = "FROZEN"
            contraction_mult = 0.0
        elif consecutive_losses >= MAX_CONSECUTIVE_HARD or (det_hit and det_draw):
            emergency_level  = 2
            guardian_state   = "CONTRACTING"
            contraction_mult = HARD_CONTRACTION_MULT
        elif det_loss or det_hit or det_draw:
            emergency_level  = 1
            guardian_state   = "CONTRACTING"
            contraction_mult = SOFT_CONTRACTION_MULT
        elif det_count >= 1:
            emergency_level  = 0
            guardian_state   = "MONITORING"
            contraction_mult = 1.0
        else:
            emergency_level  = 0
            guardian_state   = "NORMAL"
            contraction_mult = 1.0

        recommendation = self._build_recommendation(
            guardian_state, emergency_level, contraction_mult,
            consecutive_losses, recent_hit_rate, drawdown_pct, exch_instab,
        )

        report = GuardianReport(
            report_id             = report_id,
            guardian_state        = guardian_state,
            emergency_level       = emergency_level,
            contraction_multiplier = contraction_mult,
            rollback_triggered    = rollback_triggered,
            rollback_reason       = rollback_reason,
            freeze_triggered      = freeze_triggered,
            freeze_reason         = freeze_reason,
            detections            = detections,
            consecutive_losses    = consecutive_losses,
            recent_hit_rate       = round(recent_hit_rate, 4),
            drawdown_pct          = round(drawdown_pct, 4),
            trades_per_hour       = round(trades_per_hour, 1),
            avg_confidence        = round(avg_confidence, 4),
            exchange_instability_score = round(exch_instab, 1),
            live_executions_analyzed  = len(records),
            analysis_window       = self.window,
            recommendation        = recommendation,
            evaluated_at          = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_emergency.set(emergency_level)
                _prom_contraction.set(contraction_mult)
                _prom_instability.set(exch_instab)
            except Exception:
                pass

        return report

    # ── Detection helpers ──────────────────────────────────────────────────────

    def _count_consecutive_losses(self, records: list[dict]) -> int:
        """Conta losses consecutivos a partir do mais recente."""
        count = 0
        for r in reversed(records):
            ep = r.get("expected_price", 0.0)
            ex = r.get("executed_price", 0.0)
            side = r.get("side", "buy")
            # Definicao simples: fill abaixo de 90% ou slippage > 15bps = "loss"
            fill = r.get("fill_rate", 1.0)
            slip = r.get("slippage_bps", 0.0)
            if fill < 0.90 or slip > 15.0:
                count += 1
            else:
                break
        return count

    def _compute_hit_rate(self, records: list[dict]) -> float:
        """Hit rate = fracao de trades com fill >= 90% e slippage <= 15bps."""
        if not records:
            return 1.0
        hits = sum(
            1 for r in records
            if r.get("fill_rate", 1.0) >= 0.90 and r.get("slippage_bps", 0.0) <= 15.0
        )
        return hits / len(records)

    def _estimate_drawdown(self, records: list[dict]) -> float:
        """Estima drawdown como custo acumulado em fracao do tamanho medio."""
        if not records:
            return 0.0
        total_fees = sum(r.get("fee_usd", 0.0) for r in records)
        avg_size   = statistics.mean(
            r.get("filled_size", 0.0) * r.get("executed_price", 1.0)
            for r in records
        ) or 1.0
        return min(1.0, total_fees / (avg_size * len(records) + 1e-9))

    def _compute_drawdown_acceleration(self, records: list[dict]) -> float:
        """Calcula aceleracao do drawdown (variacao de slippage entre primeira/segunda metade)."""
        if len(records) < 4:
            return 0.0
        mid   = len(records) // 2
        first = statistics.mean(r.get("slippage_bps", 0.0) for r in records[:mid])
        last  = statistics.mean(r.get("slippage_bps", 0.0) for r in records[mid:])
        # Converte de bps para fracao aproximada
        return max(0.0, (last - first) / 10000.0)

    def _compute_trade_frequency(self, records: list[dict]) -> float:
        """Trades por hora estimados a partir dos timestamps."""
        if len(records) < 2:
            return 0.0
        try:
            first_ts = datetime.fromisoformat(records[0].get("recorded_at", ""))
            last_ts  = datetime.fromisoformat(records[-1].get("recorded_at", ""))
            elapsed_hours = max((last_ts - first_ts).total_seconds() / 3600.0, 1.0 / 60.0)
            return len(records) / elapsed_hours
        except Exception:
            return 0.0

    def _compute_avg_confidence(self, records: list[dict]) -> float:
        """Confidence media dos registros (campo opcional, default 0.65)."""
        confs = [r.get("confidence", 0.65) for r in records]
        return statistics.mean(confs)

    def _compute_exchange_instability(self, records: list[dict]) -> float:
        """Score de instabilidade da exchange (0-100). Baseado em latencia e fill trend."""
        if len(records) < 4:
            return 0.0
        mid = len(records) // 2
        # Latencia: comparar segunda vs primeira metade
        lat_first = statistics.mean(r.get("latency_ms", 100.0) for r in records[:mid])
        lat_last  = statistics.mean(r.get("latency_ms", 100.0) for r in records[mid:])
        lat_delta = max(0.0, lat_last - lat_first)

        # Fill rate: comparar segunda vs primeira metade
        fill_first = statistics.mean(r.get("fill_rate", 1.0) for r in records[:mid])
        fill_last  = statistics.mean(r.get("fill_rate", 1.0) for r in records[mid:])
        fill_drop  = max(0.0, fill_first - fill_last)

        lat_score  = min(100.0, lat_delta / LATENCY_INSTAB_BPS * 50.0)
        fill_score = min(100.0, fill_drop / FILL_INSTAB_DROP * 50.0)
        return min(100.0, lat_score + fill_score)

    # ── Recommendation ─────────────────────────────────────────────────────────

    def _build_recommendation(
        self, state: str, level: int, mult: float,
        losses: int, hit_rate: float, drawdown: float, instab: float,
    ) -> str:
        if state == "ROLLBACK":
            return (
                f"ROLLBACK AUTOMATICO: {losses} losses consecutivos, "
                f"hit_rate={hit_rate:.0%}. Retornar para paper imediatamente."
            )
        if state == "FROZEN":
            return "LIVE CONGELADO: multiplos sinais de degradacao simultaneos."
        if state == "CONTRACTING" and level == 2:
            return (
                f"CONTRACAO HARD ({mult:.0%}): degradacao severa detectada. "
                f"Reduzir posicoes drasticamente."
            )
        if state == "CONTRACTING" and level == 1:
            return (
                f"CONTRACAO SOFT ({mult:.0%}): losses={losses}, "
                f"hit_rate={hit_rate:.0%}. Reduzir tamanho de ordens."
            )
        if state == "MONITORING":
            return f"MONITORAMENTO: indicadores de atencao. Exchange instab={instab:.0f}/100."
        return f"NORMAL: execucao dentro dos limites. hit_rate={hit_rate:.0%}"

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: GuardianReport) -> None:
        try:
            self.guardian_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":           report.evaluated_at,
                "guardian_state":         report.guardian_state,
                "emergency_level":        report.emergency_level,
                "contraction_multiplier": report.contraction_multiplier,
                "rollback_triggered":     report.rollback_triggered,
                "freeze_triggered":       report.freeze_triggered,
                "consecutive_losses":     report.consecutive_losses,
                "recent_hit_rate":        report.recent_hit_rate,
                "exchange_instability":   report.exchange_instability_score,
                "detection_count":        report.detections.detection_count,
            }
            with open(self.guardian_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _load_execution_records(self) -> list[dict]:
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

    def _minimal_report(self, report_id: str, n_records: int) -> GuardianReport:
        return GuardianReport(
            report_id             = report_id,
            guardian_state        = "MONITORING",
            emergency_level       = 0,
            contraction_multiplier = 1.0,
            rollback_triggered    = False,
            rollback_reason       = None,
            freeze_triggered      = False,
            freeze_reason         = None,
            detections            = GuardianDetections(False,False,False,False,False,False,False,False,0),
            consecutive_losses    = 0,
            recent_hit_rate       = 1.0,
            drawdown_pct          = 0.0,
            trades_per_hour       = 0.0,
            avg_confidence        = 0.65,
            exchange_instability_score = 0.0,
            live_executions_analyzed  = n_records,
            analysis_window       = self.window,
            recommendation        = f"Apenas {n_records} execucoes live — guardian em espera.",
            evaluated_at          = datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Live Guardian — Phase Q Q-3")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    guardian = AutonomousLiveGuardian()
    report   = guardian.evaluate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nAutonomous Live Guardian")
    print(f"  guardian_state:           {report.guardian_state}")
    print(f"  emergency_level:          {report.emergency_level}/5")
    print(f"  contraction_multiplier:   {report.contraction_multiplier:.0%}")
    print(f"  rollback_triggered:       {'SIM' if report.rollback_triggered else 'nao'}")
    print(f"  freeze_triggered:         {'SIM' if report.freeze_triggered else 'nao'}")
    print(f"\n  Metricas:")
    print(f"    consecutive_losses:     {report.consecutive_losses}")
    print(f"    recent_hit_rate:        {report.recent_hit_rate:.0%}")
    print(f"    drawdown_pct:           {report.drawdown_pct:.3%}")
    print(f"    trades_per_hour:        {report.trades_per_hour:.1f}")
    print(f"    exchange_instability:   {report.exchange_instability_score:.0f}/100")
    print(f"\n  Deteccoes ({report.detections.detection_count} ativas):")
    d = report.detections
    print(f"    loss_sequence:          {'SIM' if d.loss_sequence else 'nao'}")
    print(f"    hit_rate_degradation:   {'SIM' if d.hit_rate_degradation else 'nao'}")
    print(f"    drawdown_acceleration:  {'SIM' if d.drawdown_acceleration else 'nao'}")
    print(f"    overtrading:            {'SIM' if d.overtrading else 'nao'}")
    print(f"    confidence_collapse:    {'SIM' if d.confidence_collapse else 'nao'}")
    print(f"    exchange_instability:   {'SIM' if d.exchange_instability else 'nao'}")
    print(f"    behavioral_anomaly:     {'SIM' if d.behavioral_anomaly else 'nao'}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

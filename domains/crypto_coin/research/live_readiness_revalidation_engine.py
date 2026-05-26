"""
live_readiness_revalidation_engine.py — Phase Q Q-6

Live Readiness Revalidation Engine.

Reavalia continuamente o estado de prontidao para execucao live
enquanto o sistema esta operando. Diferente do MicroLiveReadinessEngine
(que aprova entrada), este monitora se as condicoes continuam validas
durante a operacao.

Score: continuous_live_readiness_score (0-100)

Thresholds:
  GREEN  >= 75: operacao normal permitida
  YELLOW >= 55: monitoramento intensivo, sem reducao
  ORANGE >= 40: contracao automatica, alertar
  RED    <  40: rollback automatico para paper

Inputs avaliados:
  - governance_health_score   (do governance_history.jsonl)
  - execution_quality_score   (do live_execution_audit_summary.jsonl)
  - guardian_state            (do live_guardian_log.jsonl)
  - divergence_score          (do paper_vs_live_divergence_log.jsonl)
  - capital_preservation      (do live_capital_preservation_log.jsonl)
  - validation_health_score   (do validation_loop_history.jsonl)

CLI:
  python -m domains.crypto_coin.research.live_readiness_revalidation_engine
  python -m domains.crypto_coin.research.live_readiness_revalidation_engine --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

REVALIDATION_LOG = Path("data/live_readiness_revalidation_log.jsonl")

# Source logs (read-only)
GOVERNANCE_LOG    = Path("data/governance_history.jsonl")
AUDIT_SUMMARY     = Path("data/live_execution_audit_summary.jsonl")
GUARDIAN_LOG      = Path("data/live_guardian_log.jsonl")
DIVERGENCE_LOG    = Path("data/paper_vs_live_divergence_log.jsonl")
CAPITAL_LOG       = Path("data/live_capital_preservation_log.jsonl")
VALIDATION_LOG    = Path("data/validation_loop_history.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import continuous_live_readiness_score as _prom_readiness
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Thresholds ─────────────────────────────────────────────────────────────────

SCORE_GREEN  = 75.0
SCORE_YELLOW = 55.0
SCORE_ORANGE = 40.0

MIN_GOVERNANCE_HEALTH  = 65.0
MIN_EXEC_QUALITY       = 60.0
MAX_DIVERGENCE_SCORE   = 50.0
GUARDIAN_STATES_OK     = {"NORMAL", "MONITORING"}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class RevalidationInputs:
    governance_health:     float   # ultimo ciclo
    execution_quality:     float   # ultimo audit
    guardian_state:        str     # NORMAL | MONITORING | CONTRACTING | FROZEN | ROLLBACK
    divergence_score:      float   # ultimo calculo
    capital_preserved:     bool    # trading_allowed do capital engine
    validation_health:     float   # ultimo validation loop
    inputs_available:      int     # quantos inputs foram encontrados (max 6)


@dataclass
class RevalidationReport:
    """Relatorio de revalidacao continua de prontidao live."""
    report_id:                    str
    continuous_live_readiness_score: float   # 0-100
    readiness_status:             str    # GREEN | YELLOW | ORANGE | RED
    rollback_recommended:         bool
    rollback_reason:              str | None

    # Inputs
    inputs:                       RevalidationInputs

    # Penalidades aplicadas
    governance_penalty:           float
    quality_penalty:              float
    guardian_penalty:             float
    divergence_penalty:           float
    capital_penalty:              float

    # Trend
    readiness_trend:              str    # stable | improving | degrading
    cycles_below_threshold:       int    # ciclos consecutivos abaixo de ORANGE

    recommendation:               str
    evaluated_at:                 str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["inputs"] = asdict(self.inputs)
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class LiveReadinessRevalidationEngine:
    """
    Q-6: Monitoramento continuo de prontidao live.

    Agrega sinais de todos os modulos Q para determinar se o sistema
    continua apto para execucao live. Aciona rollback autonomo se RED.
    """

    def __init__(self, revalidation_log: Path = REVALIDATION_LOG):
        self.revalidation_log = revalidation_log

    def evaluate(self) -> RevalidationReport:
        """Revalida prontidao live e retorna relatorio."""
        report_id = str(uuid.uuid4())[:10]

        inputs = self._collect_inputs()
        score, penalties = self._compute_score(inputs)

        readiness_status = (
            "GREEN"  if score >= SCORE_GREEN  else
            "YELLOW" if score >= SCORE_YELLOW else
            "ORANGE" if score >= SCORE_ORANGE else
            "RED"
        )

        rollback_recommended = readiness_status == "RED"
        rollback_reason: str | None = None
        if rollback_recommended:
            rollback_reason = self._build_rollback_reason(inputs, score)

        readiness_trend    = self._compute_trend()
        cycles_below       = self._count_cycles_below_orange()

        recommendation = self._build_recommendation(
            score, readiness_status, rollback_recommended,
            inputs, rollback_reason,
        )

        report = RevalidationReport(
            report_id                    = report_id,
            continuous_live_readiness_score = round(score, 1),
            readiness_status             = readiness_status,
            rollback_recommended         = rollback_recommended,
            rollback_reason              = rollback_reason,
            inputs                       = inputs,
            governance_penalty           = round(penalties["governance"], 1),
            quality_penalty              = round(penalties["quality"], 1),
            guardian_penalty             = round(penalties["guardian"], 1),
            divergence_penalty           = round(penalties["divergence"], 1),
            capital_penalty              = round(penalties["capital"], 1),
            readiness_trend              = readiness_trend,
            cycles_below_threshold       = cycles_below,
            recommendation               = recommendation,
            evaluated_at                 = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_readiness.set(score)
            except Exception:
                pass

        return report

    # ── Input collection ───────────────────────────────────────────────────────

    def _collect_inputs(self) -> RevalidationInputs:
        gov_health  = self._read_last_float(GOVERNANCE_LOG,  "governance_health_score", 75.0)
        exec_qual   = self._read_last_float(AUDIT_SUMMARY,   "execution_quality_score", 75.0)
        guardian_st = self._read_last_str(  GUARDIAN_LOG,    "guardian_state", "MONITORING")
        divergence  = self._read_last_float(DIVERGENCE_LOG,  "divergence_score", 0.0)
        cap_ok      = self._read_last_bool( CAPITAL_LOG,     "trading_allowed", True)
        val_health  = self._read_last_float(VALIDATION_LOG,  "validation_health_score", 75.0)

        available = sum([
            GOVERNANCE_LOG.exists(),
            AUDIT_SUMMARY.exists(),
            GUARDIAN_LOG.exists(),
            DIVERGENCE_LOG.exists(),
            CAPITAL_LOG.exists(),
            VALIDATION_LOG.exists(),
        ])

        return RevalidationInputs(
            governance_health = gov_health,
            execution_quality = exec_qual,
            guardian_state    = guardian_st,
            divergence_score  = divergence,
            capital_preserved = cap_ok,
            validation_health = val_health,
            inputs_available  = available,
        )

    # ── Score computation ──────────────────────────────────────────────────────

    def _compute_score(self, inp: RevalidationInputs) -> tuple[float, dict]:
        score = 100.0
        penalties: dict[str, float] = {}

        # Governance health (max penalty 25)
        gov_pen = 0.0
        if inp.governance_health < MIN_GOVERNANCE_HEALTH:
            gov_pen = min(25.0, (MIN_GOVERNANCE_HEALTH - inp.governance_health) * 0.8)
        penalties["governance"] = gov_pen
        score -= gov_pen

        # Execution quality (max penalty 20)
        qual_pen = 0.0
        if inp.execution_quality < MIN_EXEC_QUALITY:
            qual_pen = min(20.0, (MIN_EXEC_QUALITY - inp.execution_quality) * 0.5)
        penalties["quality"] = qual_pen
        score -= qual_pen

        # Guardian state (max penalty 30)
        guardian_pen = {
            "NORMAL":      0.0,
            "MONITORING":  5.0,
            "CONTRACTING": 15.0,
            "FROZEN":      25.0,
            "ROLLBACK":    30.0,
        }.get(inp.guardian_state, 10.0)
        penalties["guardian"] = guardian_pen
        score -= guardian_pen

        # Divergence score (max penalty 15)
        div_pen = 0.0
        if inp.divergence_score > MAX_DIVERGENCE_SCORE:
            div_pen = min(15.0, (inp.divergence_score - MAX_DIVERGENCE_SCORE) * 0.3)
        penalties["divergence"] = div_pen
        score -= div_pen

        # Capital preservation (max penalty 20)
        cap_pen = 0.0 if inp.capital_preserved else 20.0
        penalties["capital"] = cap_pen
        score -= cap_pen

        return max(0.0, min(100.0, score)), penalties

    # ── Trend & history ────────────────────────────────────────────────────────

    def _compute_trend(self) -> str:
        history = self._load_recent_scores(n=6)
        if len(history) < 3:
            return "stable"
        mid        = len(history) // 2
        avg_first  = sum(history[:mid]) / mid
        avg_last   = sum(history[mid:]) / (len(history) - mid)
        delta      = avg_last - avg_first
        if abs(delta) < 3.0:
            return "stable"
        return "improving" if delta > 0 else "degrading"

    def _count_cycles_below_orange(self) -> int:
        history = self._load_recent_scores(n=10)
        count = 0
        for s in reversed(history):
            if s < SCORE_ORANGE:
                count += 1
            else:
                break
        return count

    def _load_recent_scores(self, n: int = 10) -> list[float]:
        return [
            r.get("continuous_live_readiness_score", 75.0)
            for r in self._load_log(self.revalidation_log, n)
        ]

    def _build_rollback_reason(self, inp: RevalidationInputs, score: float) -> str:
        reasons = []
        if inp.governance_health < MIN_GOVERNANCE_HEALTH:
            reasons.append(f"governance={inp.governance_health:.0f}")
        if inp.execution_quality < MIN_EXEC_QUALITY:
            reasons.append(f"exec_quality={inp.execution_quality:.0f}")
        if inp.guardian_state not in GUARDIAN_STATES_OK:
            reasons.append(f"guardian={inp.guardian_state}")
        if inp.divergence_score > MAX_DIVERGENCE_SCORE:
            reasons.append(f"divergence={inp.divergence_score:.0f}")
        if not inp.capital_preserved:
            reasons.append("capital_halt")
        return f"score={score:.0f} ({'; '.join(reasons)})"

    def _build_recommendation(
        self, score: float, status: str, rollback: bool,
        inp: RevalidationInputs, rollback_reason: str | None,
    ) -> str:
        if rollback:
            return (
                f"ROLLBACK RECOMENDADO: readiness={score:.0f}/100 [{status}]. "
                f"Razao: {rollback_reason}. Retornar para paper imediatamente."
            )
        if status == "ORANGE":
            return (
                f"ALERTA ORANGE ({score:.0f}/100): contracao automatica em efeito. "
                f"Guardian={inp.guardian_state} divergence={inp.divergence_score:.0f}. "
                "Monitoramento intensivo."
            )
        if status == "YELLOW":
            return (
                f"ATENCAO ({score:.0f}/100): indicadores de risco presentes. "
                "Monitorar proximo ciclo."
            )
        return f"Sistema apto para live ({score:.0f}/100) [{status}]. Operacao normal."

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _read_last_float(self, path: Path, key: str, default: float) -> float:
        records = self._load_log(path, n=1)
        if records:
            return float(records[-1].get(key, default))
        return default

    def _read_last_str(self, path: Path, key: str, default: str) -> str:
        records = self._load_log(path, n=1)
        if records:
            return str(records[-1].get(key, default))
        return default

    def _read_last_bool(self, path: Path, key: str, default: bool) -> bool:
        records = self._load_log(path, n=1)
        if records:
            val = records[-1].get(key, default)
            return bool(val)
        return default

    def _load_log(self, path: Path, n: int = 5) -> list[dict]:
        if not path.exists():
            return []
        records: list[dict] = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return records[-n:]

    def _persist(self, report: RevalidationReport) -> None:
        try:
            self.revalidation_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                  report.evaluated_at,
                "continuous_live_readiness_score": report.continuous_live_readiness_score,
                "readiness_status":              report.readiness_status,
                "rollback_recommended":          report.rollback_recommended,
                "governance_penalty":            report.governance_penalty,
                "quality_penalty":               report.quality_penalty,
                "guardian_penalty":              report.guardian_penalty,
                "divergence_penalty":            report.divergence_penalty,
                "capital_penalty":               report.capital_penalty,
                "readiness_trend":               report.readiness_trend,
                "cycles_below_threshold":        report.cycles_below_threshold,
            }
            with open(self.revalidation_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live Readiness Revalidation Engine — Phase Q Q-6"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = LiveReadinessRevalidationEngine()
    report = engine.evaluate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    status_icons = {"GREEN": "✓", "YELLOW": "~", "ORANGE": "!", "RED": "X"}
    icon = status_icons.get(report.readiness_status, "?")

    print(f"\nLive Readiness Revalidation Engine")
    print(f"  continuous_live_readiness: {report.continuous_live_readiness_score:.1f}/100")
    print(f"  readiness_status:          [{icon}] {report.readiness_status}")
    print(f"  rollback_recommended:      {'SIM' if report.rollback_recommended else 'nao'}")
    print(f"  readiness_trend:           {report.readiness_trend}")
    print(f"  cycles_below_threshold:    {report.cycles_below_threshold}")
    if report.rollback_reason:
        print(f"\n  Rollback reason: {report.rollback_reason}")
    print(f"\n  Inputs ({report.inputs.inputs_available}/6 disponiveis):")
    i = report.inputs
    print(f"    governance_health:  {i.governance_health:.1f}")
    print(f"    execution_quality:  {i.execution_quality:.1f}")
    print(f"    guardian_state:     {i.guardian_state}")
    print(f"    divergence_score:   {i.divergence_score:.1f}")
    print(f"    capital_preserved:  {'sim' if i.capital_preserved else 'NAO'}")
    print(f"    validation_health:  {i.validation_health:.1f}")
    print(f"\n  Penalidades:")
    print(f"    governance: -{report.governance_penalty:.1f}")
    print(f"    quality:    -{report.quality_penalty:.1f}")
    print(f"    guardian:   -{report.guardian_penalty:.1f}")
    print(f"    divergence: -{report.divergence_penalty:.1f}")
    print(f"    capital:    -{report.capital_penalty:.1f}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

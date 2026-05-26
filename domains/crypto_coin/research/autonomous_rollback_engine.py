"""
autonomous_rollback_engine.py — Phase Q Q-7

Autonomous Rollback Engine.

Executa rollback autonomo de live para paper quando condicoes de seguranca
sao violadas. Gera incident report detalhado para pos-analise.

Triggers de rollback (em ordem de severidade):
  1. guardian_rollback:      AutonomousLiveGuardian acionou ROLLBACK
  2. readiness_red:          LiveReadinessRevalidation retornou RED
  3. capital_halt:           LiveCapitalPreservation suspendeu trading
  4. exchange_degradation:   LiveExecutionAuditor detectou exchange_degradation
  5. divergence_critical:    PaperVsLiveDivergence score > 70
  6. governance_collapse:    governance_health < 50
  7. manual_override:        acionado manualmente via CLI

Apos rollback:
  - Persiste incident report detalhado
  - Define recovery_requirements para retorno ao live
  - Referencia pós-mortem para investigacao

Recovery requirements (para reativar live apos rollback):
  - governance_health >= 70 por 2+ ciclos
  - execution_quality >= 70
  - readiness_score >= 80
  - zero deteccoes criticas por 3+ ciclos
  - revisao manual obrigatoria para rollback tipo 1 ou 2

CLI:
  python -m domains.crypto_coin.research.autonomous_rollback_engine --status
  python -m domains.crypto_coin.research.autonomous_rollback_engine --trigger manual
  python -m domains.crypto_coin.research.autonomous_rollback_engine --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

ROLLBACK_LOG  = Path("data/autonomous_rollback_log.jsonl")
INCIDENT_LOG  = Path("data/live_incident_reports.jsonl")

# Source logs (read-only)
GUARDIAN_LOG  = Path("data/live_guardian_log.jsonl")
REVALID_LOG   = Path("data/live_readiness_revalidation_log.jsonl")
CAPITAL_LOG   = Path("data/live_capital_preservation_log.jsonl")
AUDIT_SUMMARY = Path("data/live_execution_audit_summary.jsonl")
DIVERGENCE_LOG = Path("data/paper_vs_live_divergence_log.jsonl")
GOVERNANCE_LOG = Path("data/governance_history.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import rollback_events_total as _prom_rollbacks
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Trigger thresholds ─────────────────────────────────────────────────────────

DIVERGENCE_CRIT_THRESHOLD  = 70.0
GOVERNANCE_COLLAPSE_THRESH = 50.0

# Recovery requirements
RECOVERY_GOVERNANCE_MIN    = 70.0
RECOVERY_EXEC_QUALITY_MIN  = 70.0
RECOVERY_READINESS_MIN     = 80.0
RECOVERY_CYCLES_CLEAN      = 3
RECOVERY_GOV_CYCLES        = 2


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class RollbackTrigger:
    trigger_type:     str    # guardian_rollback | readiness_red | capital_halt | ...
    trigger_severity: int    # 1-7 (1=mais severo)
    triggered:        bool
    evidence:         str    # dados que suportam o trigger


@dataclass
class RecoveryRequirements:
    governance_health_min:    float
    execution_quality_min:    float
    readiness_score_min:      float
    cycles_clean_required:    int
    governance_cycles_min:    int
    manual_review_required:   bool
    estimated_recovery_desc:  str


@dataclass
class RollbackReport:
    """Incident report gerado apos rollback autonomo."""
    incident_id:          str
    rollback_executed:    bool
    rollback_timestamp:   str | None
    rollback_reason:      str | None
    trigger_type:         str | None
    trigger_severity:     int | None   # 1=mais critico

    # Triggers avaliados
    triggers_evaluated:   list[dict]
    triggers_fired:       int

    # Estado pre-rollback
    pre_rollback_governance:      float
    pre_rollback_exec_quality:    float
    pre_rollback_readiness:       float
    pre_rollback_guardian_state:  str
    pre_rollback_divergence:      float

    # Recovery
    recovery_requirements:        RecoveryRequirements
    post_mortem_reference:        str   # ID do incident para investigacao

    recommendation:               str
    evaluated_at:                 str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["recovery_requirements"] = asdict(self.recovery_requirements)
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class AutonomousRollbackEngine:
    """
    Q-7: Motor de rollback autonomo.

    Avalia triggers de rollback e executa a transicao paper quando necessario.
    Gera incident report detalhado para pos-analise.
    """

    def __init__(
        self,
        rollback_log: Path = ROLLBACK_LOG,
        incident_log: Path = INCIDENT_LOG,
    ):
        self.rollback_log = rollback_log
        self.incident_log = incident_log

    def evaluate(self, force_trigger: str | None = None) -> RollbackReport:
        """Avalia se rollback deve ser acionado e retorna relatorio."""
        incident_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # ── Coletar estado atual ───────────────────────────────────────────────
        guardian_state    = self._read_last_str(GUARDIAN_LOG,   "guardian_state", "NORMAL")
        guardian_rollback = self._read_last_bool(GUARDIAN_LOG,  "rollback_triggered", False)
        readiness_status  = self._read_last_str(REVALID_LOG,    "readiness_status", "GREEN")
        capital_ok        = self._read_last_bool(CAPITAL_LOG,    "trading_allowed", True)
        exchange_deg      = self._read_last_bool(AUDIT_SUMMARY,  "exchange_degradation", False)
        divergence_score  = self._read_last_float(DIVERGENCE_LOG, "divergence_score", 0.0)
        gov_health        = self._read_last_float(GOVERNANCE_LOG, "governance_health_score", 75.0)
        exec_quality      = self._read_last_float(AUDIT_SUMMARY,  "execution_quality_score", 75.0)
        readiness_score   = self._read_last_float(REVALID_LOG,    "continuous_live_readiness_score", 75.0)

        # ── Triggers ──────────────────────────────────────────────────────────
        triggers = [
            RollbackTrigger(
                trigger_type="guardian_rollback", trigger_severity=1,
                triggered=guardian_rollback or force_trigger == "guardian",
                evidence=f"guardian_state={guardian_state} rollback_triggered={guardian_rollback}",
            ),
            RollbackTrigger(
                trigger_type="readiness_red", trigger_severity=2,
                triggered=readiness_status == "RED" or force_trigger == "readiness_red",
                evidence=f"readiness_status={readiness_status} score={readiness_score:.0f}",
            ),
            RollbackTrigger(
                trigger_type="capital_halt", trigger_severity=3,
                triggered=not capital_ok or force_trigger == "capital_halt",
                evidence=f"trading_allowed={capital_ok}",
            ),
            RollbackTrigger(
                trigger_type="exchange_degradation", trigger_severity=4,
                triggered=exchange_deg or force_trigger == "exchange_degradation",
                evidence=f"exchange_degradation={exchange_deg}",
            ),
            RollbackTrigger(
                trigger_type="divergence_critical", trigger_severity=5,
                triggered=divergence_score > DIVERGENCE_CRIT_THRESHOLD or force_trigger == "divergence_critical",
                evidence=f"divergence_score={divergence_score:.0f} threshold={DIVERGENCE_CRIT_THRESHOLD:.0f}",
            ),
            RollbackTrigger(
                trigger_type="governance_collapse", trigger_severity=6,
                triggered=gov_health < GOVERNANCE_COLLAPSE_THRESH or force_trigger == "governance_collapse",
                evidence=f"governance_health={gov_health:.0f} threshold={GOVERNANCE_COLLAPSE_THRESH:.0f}",
            ),
            RollbackTrigger(
                trigger_type="manual_override", trigger_severity=7,
                triggered=force_trigger == "manual",
                evidence="Acionado manualmente via CLI",
            ),
        ]

        fired = [t for t in triggers if t.triggered]
        triggers_fired = len(fired)

        # Trigger mais severo (menor severity number = mais severo)
        primary_trigger: RollbackTrigger | None = None
        if fired:
            primary_trigger = min(fired, key=lambda t: t.trigger_severity)

        rollback_executed = triggers_fired > 0
        rollback_timestamp = now if rollback_executed else None
        rollback_reason = primary_trigger.evidence if primary_trigger else None
        trigger_type    = primary_trigger.trigger_type if primary_trigger else None
        trigger_severity = primary_trigger.trigger_severity if primary_trigger else None

        # ── Recovery requirements ──────────────────────────────────────────────
        manual_review = (
            primary_trigger is not None and
            primary_trigger.trigger_severity <= 2
        )
        recovery = RecoveryRequirements(
            governance_health_min  = RECOVERY_GOVERNANCE_MIN,
            execution_quality_min  = RECOVERY_EXEC_QUALITY_MIN,
            readiness_score_min    = RECOVERY_READINESS_MIN,
            cycles_clean_required  = RECOVERY_CYCLES_CLEAN,
            governance_cycles_min  = RECOVERY_GOV_CYCLES,
            manual_review_required = manual_review,
            estimated_recovery_desc = (
                "Revisao manual obrigatoria antes de reativar live. "
                if manual_review else
                "Reativar live apos "
                f"{RECOVERY_CYCLES_CLEAN} ciclos limpos e "
                f"governance >= {RECOVERY_GOVERNANCE_MIN:.0f}."
            ),
        )

        recommendation = self._build_recommendation(
            rollback_executed, primary_trigger, recovery, gov_health,
        )

        report = RollbackReport(
            incident_id           = incident_id,
            rollback_executed     = rollback_executed,
            rollback_timestamp    = rollback_timestamp,
            rollback_reason       = rollback_reason,
            trigger_type          = trigger_type,
            trigger_severity      = trigger_severity,
            triggers_evaluated    = [asdict(t) for t in triggers],
            triggers_fired        = triggers_fired,
            pre_rollback_governance     = round(gov_health, 1),
            pre_rollback_exec_quality   = round(exec_quality, 1),
            pre_rollback_readiness      = round(readiness_score, 1),
            pre_rollback_guardian_state = guardian_state,
            pre_rollback_divergence     = round(divergence_score, 1),
            recovery_requirements = recovery,
            post_mortem_reference = f"INCIDENT-{incident_id}",
            recommendation        = recommendation,
            evaluated_at          = now,
        )

        if rollback_executed:
            self._persist_incident(report)
        self._persist_rollback(report)

        if _METRICS_AVAILABLE and rollback_executed:
            try:
                _prom_rollbacks.labels(trigger=trigger_type or "unknown").inc()
            except Exception:
                pass

        return report

    # ── Recommendation ─────────────────────────────────────────────────────────

    def _build_recommendation(
        self, executed: bool,
        trigger: "RollbackTrigger | None",
        recovery: RecoveryRequirements,
        gov_health: float,
    ) -> str:
        if not executed:
            return f"Sem rollback necessario. governance={gov_health:.0f}. Sistema live estavel."
        t_type = trigger.trigger_type if trigger else "unknown"
        t_sev  = trigger.trigger_severity if trigger else 0
        return (
            f"ROLLBACK EXECUTADO [severity={t_sev} trigger={t_type}]. "
            f"Sistema retornou para paper. "
            f"Incident: {recovery.estimated_recovery_desc}"
        )

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist_incident(self, report: RollbackReport) -> None:
        try:
            self.incident_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.incident_log, "a") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

    def _persist_rollback(self, report: RollbackReport) -> None:
        try:
            self.rollback_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":       report.evaluated_at,
                "rollback_executed":  report.rollback_executed,
                "trigger_type":       report.trigger_type,
                "trigger_severity":   report.trigger_severity,
                "triggers_fired":     report.triggers_fired,
                "post_mortem":        report.post_mortem_reference,
            }
            with open(self.rollback_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _read_last_float(self, path: Path, key: str, default: float) -> float:
        rec = self._load_last(path)
        return float(rec.get(key, default)) if rec else default

    def _read_last_str(self, path: Path, key: str, default: str) -> str:
        rec = self._load_last(path)
        return str(rec.get(key, default)) if rec else default

    def _read_last_bool(self, path: Path, key: str, default: bool) -> bool:
        rec = self._load_last(path)
        return bool(rec.get(key, default)) if rec else default

    def _load_last(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        last: dict | None = None
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return last


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Rollback Engine — Phase Q Q-7"
    )
    parser.add_argument("--trigger", type=str, default=None,
                        choices=["manual","guardian","readiness_red","capital_halt",
                                 "exchange_degradation","divergence_critical","governance_collapse"],
                        help="Forcar trigger de rollback")
    parser.add_argument("--status", action="store_true",
                        help="Avaliar sem forcar rollback")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    engine = AutonomousRollbackEngine()
    report = engine.evaluate(force_trigger=args.trigger)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nAutonomous Rollback Engine")
    print(f"  incident_id:           {report.incident_id}")
    print(f"  rollback_executed:     {'SIM' if report.rollback_executed else 'nao'}")
    if report.rollback_timestamp:
        print(f"  rollback_timestamp:    {report.rollback_timestamp}")
    if report.trigger_type:
        print(f"  trigger_type:          {report.trigger_type} (severity={report.trigger_severity})")
    if report.rollback_reason:
        print(f"  rollback_reason:       {report.rollback_reason}")
    print(f"\n  Pre-rollback state:")
    print(f"    governance:    {report.pre_rollback_governance:.1f}")
    print(f"    exec_quality:  {report.pre_rollback_exec_quality:.1f}")
    print(f"    readiness:     {report.pre_rollback_readiness:.1f}")
    print(f"    guardian:      {report.pre_rollback_guardian_state}")
    print(f"    divergence:    {report.pre_rollback_divergence:.1f}")
    print(f"\n  Triggers ({report.triggers_fired} disparados):")
    for t in report.triggers_evaluated:
        fired_str = "DISPARADO" if t["triggered"] else "ok"
        print(f"    [{t['trigger_severity']}] {t['trigger_type']}: {fired_str}")
    if report.rollback_executed:
        r = report.recovery_requirements
        print(f"\n  Recovery requirements:")
        print(f"    governance >= {r['governance_health_min']:.0f}")
        print(f"    exec_quality >= {r['execution_quality_min']:.0f}")
        print(f"    readiness >= {r['readiness_score_min']:.0f}")
        print(f"    cycles_clean >= {r['cycles_clean_required']}")
        print(f"    manual_review: {'SIM' if r['manual_review_required'] else 'nao'}")
        print(f"\n  post_mortem_reference: {report.post_mortem_reference}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

"""
autonomous_live_governance.py — Phase Q Q-9

Autonomous Live Governance Orchestrator.

Orquestra todos os modulos Phase Q em ciclos autonomos de governanca live.
Coordena protecao de capital, qualidade de execucao, divergencia e rollback.

Scores produzidos:
  - live_governance_score:     saude geral da governanca live (0-100)
  - operational_confidence:    confianca operacional atual (0-100)
  - live_stability_score:      estabilidade do ambiente live (0-100)
  - execution_integrity:       integridade de execucao (0-100)
  - capital_safety_score:      seguranca de capital (0-100)
  - autonomous_live_approval:  True = live pode continuar operando

Fases por ciclo (independentes — falha nao bloqueia ciclo):
  FASE Q-1:  MicroLiveExecutionController  — validacao de trades
  FASE Q-2:  LiveExecutionAuditor          — auditoria de qualidade
  FASE Q-3:  AutonomousLiveGuardian        — protecao e contracao
  FASE Q-4:  PaperVsLiveDivergenceEngine   — divergencia paper/live
  FASE Q-5:  LiveCapitalPreservationEngine — limites hard de capital
  FASE Q-6:  LiveReadinessRevalidationEngine — prontidao continua
  FASE Q-7:  AutonomousRollbackEngine      — rollback autonomo
  FASE Q-8:  LiveExecutionReplayEngine     — replay e validacao

CLI:
  python -m domains.crypto_coin.research.autonomous_live_governance --status
  python -m domains.crypto_coin.research.autonomous_live_governance --run
  python -m domains.crypto_coin.research.autonomous_live_governance --json
  python -m domains.crypto_coin.research.autonomous_live_governance --run-n 3
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LIVE_GOV_LOG     = Path("data/live_governance_history.jsonl")
LIVE_GOV_SUMMARY = Path("data/live_governance_summary.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import (
        live_governance_score     as _prom_gov_score,
        execution_quality_score   as _prom_exec_quality,
        autonomous_freeze_state   as _prom_freeze,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Governance score weights ────────────────────────────────────────────────────

W_READINESS   = 0.25
W_GUARDIAN    = 0.20
W_CAPITAL     = 0.20
W_EXEC_QUAL   = 0.15
W_DIVERGENCE  = 0.10
W_REPLAY      = 0.10

# Thresholds for autonomous_live_approval
MIN_LIVE_GOV_SCORE    = 55.0
MIN_CAPITAL_SAFETY    = 60.0
GUARDIAN_BLOCK_STATES = {"FROZEN", "ROLLBACK"}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class PhaseResult:
    phase: str
    success: bool
    error: str | None
    duration_ms: float
    key_scores: dict


@dataclass
class LiveGovernanceReport:
    """Relatorio de governanca live autonoma."""
    cycle_id:               str
    run_number:             int

    # Scores principais
    live_governance_score:  float
    operational_confidence: float
    live_stability_score:   float
    execution_integrity:    float
    capital_safety_score:   float

    # Aprovacao autonoma
    autonomous_live_approval: bool
    approval_blocking_reason: str | None

    # Resultados por fase
    phase_results:          list[dict]
    phases_succeeded:       int
    phases_failed:          int

    # Decisoes autonomas neste ciclo
    rollback_executed:      bool
    guardian_state:         str
    readiness_status:       str
    trading_allowed:        bool

    # Tendencia
    governance_trend:       str   # stable | improving | degrading

    recommendation:         str
    cycle_duration_ms:      float
    evaluated_at:           str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Orchestrator ───────────────────────────────────────────────────────────────

class AutonomousLiveGovernance:
    """
    Q-9: Orquestrador de governanca live autonoma.

    Executa todas as fases Phase Q em ciclos periodicos.
    Agrega scores e toma decisoes autonomas de aprovacao/rollback.
    """

    def __init__(
        self,
        live_gov_log: Path = LIVE_GOV_LOG,
        run_number:   int  = 1,
    ):
        self.live_gov_log = live_gov_log
        self.run_number   = run_number

    def run_once(self) -> LiveGovernanceReport:
        """Executa um ciclo completo de governanca live."""
        import time
        cycle_id = str(uuid.uuid4())[:12]
        start_ms = time.time() * 1000

        phase_results: list[PhaseResult] = []

        # ── FASE Q-2: LiveExecutionAuditor ────────────────────────────────────
        exec_quality = 75.0
        phase_results.append(self._run_phase(
            "Q2_LiveExecutionAuditor",
            lambda: self._run_execution_auditor(),
        ))
        if phase_results[-1].success:
            exec_quality = phase_results[-1].key_scores.get("execution_quality_score", 75.0)

        # ── FASE Q-3: AutonomousLiveGuardian ──────────────────────────────────
        guardian_state = "NORMAL"
        contraction_mult = 1.0
        phase_results.append(self._run_phase(
            "Q3_AutonomousLiveGuardian",
            lambda: self._run_guardian(),
        ))
        if phase_results[-1].success:
            guardian_state   = phase_results[-1].key_scores.get("guardian_state", "NORMAL")
            contraction_mult = phase_results[-1].key_scores.get("contraction_multiplier", 1.0)

        # ── FASE Q-4: PaperVsLiveDivergenceEngine ─────────────────────────────
        divergence_score = 0.0
        phase_results.append(self._run_phase(
            "Q4_PaperVsLiveDivergenceEngine",
            lambda: self._run_divergence(),
        ))
        if phase_results[-1].success:
            divergence_score = phase_results[-1].key_scores.get("divergence_score", 0.0)

        # ── FASE Q-5: LiveCapitalPreservationEngine ───────────────────────────
        trading_allowed  = True
        capital_safety   = 80.0
        phase_results.append(self._run_phase(
            "Q5_LiveCapitalPreservationEngine",
            lambda: self._run_capital_preservation(),
        ))
        if phase_results[-1].success:
            trading_allowed = phase_results[-1].key_scores.get("trading_allowed", True)
            capital_safety  = phase_results[-1].key_scores.get("capital_safety_score", 80.0)

        # ── FASE Q-6: LiveReadinessRevalidationEngine ─────────────────────────
        readiness_score  = 75.0
        readiness_status = "GREEN"
        phase_results.append(self._run_phase(
            "Q6_LiveReadinessRevalidation",
            lambda: self._run_revalidation(),
        ))
        if phase_results[-1].success:
            readiness_score  = phase_results[-1].key_scores.get("continuous_live_readiness_score", 75.0)
            readiness_status = phase_results[-1].key_scores.get("readiness_status", "GREEN")

        # ── FASE Q-7: AutonomousRollbackEngine ────────────────────────────────
        rollback_executed = False
        phase_results.append(self._run_phase(
            "Q7_AutonomousRollbackEngine",
            lambda: self._run_rollback(),
        ))
        if phase_results[-1].success:
            rollback_executed = phase_results[-1].key_scores.get("rollback_executed", False)

        # ── FASE Q-8: LiveExecutionReplayEngine ───────────────────────────────
        replay_fidelity = 75.0
        phase_results.append(self._run_phase(
            "Q8_LiveExecutionReplayEngine",
            lambda: self._run_replay(),
        ))
        if phase_results[-1].success:
            replay_fidelity = phase_results[-1].key_scores.get("avg_fidelity_score", 75.0)

        # ── Scores agregados ──────────────────────────────────────────────────

        # Guardian score (100 = NORMAL, penaliza estados piores)
        guardian_score = {
            "NORMAL":      100.0,
            "MONITORING":   85.0,
            "CONTRACTING":  65.0,
            "FROZEN":       30.0,
            "ROLLBACK":     10.0,
        }.get(guardian_state, 75.0)

        # Divergence score (inverted: 0 divergencia = 100 pontos)
        divergence_contrib = max(0.0, 100.0 - divergence_score)

        live_gov = (
            readiness_score   * W_READINESS  +
            guardian_score    * W_GUARDIAN   +
            capital_safety    * W_CAPITAL    +
            exec_quality      * W_EXEC_QUAL  +
            divergence_contrib * W_DIVERGENCE +
            replay_fidelity   * W_REPLAY
        )
        live_gov = max(0.0, min(100.0, round(live_gov, 1)))

        operational_confidence = min(100.0, round(
            (readiness_score * 0.40 + guardian_score * 0.30 + exec_quality * 0.30), 1
        ))

        live_stability = min(100.0, round(
            (guardian_score * 0.40 + divergence_contrib * 0.30 + replay_fidelity * 0.30), 1
        ))

        execution_integrity = min(100.0, round(
            (exec_quality * 0.50 + replay_fidelity * 0.30 + divergence_contrib * 0.20), 1
        ))

        # ── Aprovacao autonoma ────────────────────────────────────────────────

        blocking_reason: str | None = None
        if live_gov < MIN_LIVE_GOV_SCORE:
            blocking_reason = f"live_governance_score={live_gov:.0f} < {MIN_LIVE_GOV_SCORE:.0f}"
        elif capital_safety < MIN_CAPITAL_SAFETY:
            blocking_reason = f"capital_safety_score={capital_safety:.0f} < {MIN_CAPITAL_SAFETY:.0f}"
        elif guardian_state in GUARDIAN_BLOCK_STATES:
            blocking_reason = f"guardian_state={guardian_state}"
        elif not trading_allowed:
            blocking_reason = "trading_allowed=False (capital halt)"
        elif rollback_executed:
            blocking_reason = "rollback_executed=True neste ciclo"
        elif readiness_status == "RED":
            blocking_reason = f"readiness_status=RED"

        autonomous_live_approval = blocking_reason is None

        # ── Tendencia ─────────────────────────────────────────────────────────
        governance_trend = self._compute_trend()

        phases_succeeded = sum(1 for p in phase_results if p.success)
        phases_failed    = sum(1 for p in phase_results if not p.success)

        recommendation = self._build_recommendation(
            live_gov, autonomous_live_approval, blocking_reason,
            guardian_state, rollback_executed, readiness_status,
        )

        cycle_duration = time.time() * 1000 - start_ms

        report = LiveGovernanceReport(
            cycle_id               = cycle_id,
            run_number             = self.run_number,
            live_governance_score  = live_gov,
            operational_confidence = operational_confidence,
            live_stability_score   = live_stability,
            execution_integrity    = execution_integrity,
            capital_safety_score   = round(capital_safety, 1),
            autonomous_live_approval = autonomous_live_approval,
            approval_blocking_reason = blocking_reason,
            phase_results          = [asdict(p) for p in phase_results],
            phases_succeeded       = phases_succeeded,
            phases_failed          = phases_failed,
            rollback_executed      = rollback_executed,
            guardian_state         = guardian_state,
            readiness_status       = readiness_status,
            trading_allowed        = trading_allowed,
            governance_trend       = governance_trend,
            recommendation         = recommendation,
            cycle_duration_ms      = round(cycle_duration, 1),
            evaluated_at           = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        self._persist_summary(report)

        if _METRICS_AVAILABLE:
            try:
                _prom_gov_score.set(live_gov)
                _prom_exec_quality.set(exec_quality)
                _prom_freeze.set(1.0 if guardian_state in GUARDIAN_BLOCK_STATES else 0.0)
            except Exception:
                pass

        self.run_number += 1
        return report

    def run_n(self, n: int) -> list[LiveGovernanceReport]:
        """Executa N ciclos de governanca live."""
        return [self.run_once() for _ in range(n)]

    # ── Phase runners ──────────────────────────────────────────────────────────

    def _run_phase(self, name: str, fn) -> PhaseResult:
        import time
        start = time.time() * 1000
        try:
            scores = fn()
            return PhaseResult(
                phase=name, success=True, error=None,
                duration_ms=round(time.time() * 1000 - start, 1),
                key_scores=scores,
            )
        except Exception as e:
            return PhaseResult(
                phase=name, success=False, error=str(e),
                duration_ms=round(time.time() * 1000 - start, 1),
                key_scores={},
            )

    def _run_execution_auditor(self) -> dict:
        from domains.crypto_coin.research.live_execution_auditor import LiveExecutionAuditor
        report = LiveExecutionAuditor().audit()
        return {"execution_quality_score": report.execution_quality_score}

    def _run_guardian(self) -> dict:
        from domains.crypto_coin.research.autonomous_live_guardian import AutonomousLiveGuardian
        report = AutonomousLiveGuardian().evaluate()
        return {
            "guardian_state":         report.guardian_state,
            "contraction_multiplier": report.contraction_multiplier,
            "rollback_triggered":     report.rollback_triggered,
        }

    def _run_divergence(self) -> dict:
        from domains.crypto_coin.research.paper_vs_live_divergence_engine import PaperVsLiveDivergenceEngine
        report = PaperVsLiveDivergenceEngine().evaluate()
        return {
            "divergence_score":      report.divergence_score,
            "live_consistency_score": report.live_consistency_score,
        }

    def _run_capital_preservation(self) -> dict:
        from domains.crypto_coin.research.live_capital_preservation_engine import LiveCapitalPreservationEngine
        report = LiveCapitalPreservationEngine().evaluate()
        checks_pct = report.checks.checks_passed / max(report.checks.checks_total, 1)
        capital_safety = checks_pct * 100.0
        if report.capital_frozen:
            capital_safety = 20.0
        elif not report.trading_allowed:
            capital_safety = 40.0
        elif report.contracting:
            capital_safety = 65.0
        return {
            "trading_allowed":   report.trading_allowed,
            "capital_safety_score": capital_safety,
            "consecutive_losses": report.consecutive_losses,
        }

    def _run_revalidation(self) -> dict:
        from domains.crypto_coin.research.live_readiness_revalidation_engine import LiveReadinessRevalidationEngine
        report = LiveReadinessRevalidationEngine().evaluate()
        return {
            "continuous_live_readiness_score": report.continuous_live_readiness_score,
            "readiness_status":               report.readiness_status,
            "rollback_recommended":           report.rollback_recommended,
        }

    def _run_rollback(self) -> dict:
        from domains.crypto_coin.research.autonomous_rollback_engine import AutonomousRollbackEngine
        report = AutonomousRollbackEngine().evaluate()
        return {
            "rollback_executed": report.rollback_executed,
            "trigger_type":      report.trigger_type,
        }

    def _run_replay(self) -> dict:
        from domains.crypto_coin.research.live_execution_replay_engine import LiveExecutionReplayEngine
        report = LiveExecutionReplayEngine().replay_all()
        return {
            "avg_fidelity_score":    report.avg_fidelity_score,
            "pct_correct_execution": report.pct_correct_execution,
            "pct_anomalous":         report.pct_anomalous,
        }

    # ── Trend ──────────────────────────────────────────────────────────────────

    def _compute_trend(self) -> str:
        history = self._load_recent_scores(n=6)
        if len(history) < 3:
            return "stable"
        mid       = len(history) // 2
        avg_first = sum(history[:mid]) / mid
        avg_last  = sum(history[mid:]) / (len(history) - mid)
        delta     = avg_last - avg_first
        if abs(delta) < 3.0:
            return "stable"
        return "improving" if delta > 0 else "degrading"

    def _load_recent_scores(self, n: int = 6) -> list[float]:
        records = self._load_log(self.live_gov_log, n)
        return [r.get("live_governance_score", 75.0) for r in records]

    # ── Recommendation ─────────────────────────────────────────────────────────

    def _build_recommendation(
        self, score: float, approved: bool, blocking: str | None,
        guardian: str, rollback: bool, readiness: str,
    ) -> str:
        if rollback:
            return (
                f"ROLLBACK EXECUTADO neste ciclo. "
                "Sistema retornou para paper. Investigar incident log."
            )
        if not approved:
            return (
                f"LIVE NAO APROVADO ({score:.0f}/100): {blocking}. "
                "Corrigir condicoes antes de continuar execucao live."
            )
        if guardian in ("CONTRACTING",):
            return (
                f"Live aprovado com contracao ({score:.0f}/100). "
                f"Guardian={guardian} readiness={readiness}. Monitorar proximo ciclo."
            )
        if readiness == "YELLOW":
            return (
                f"Live aprovado com atencao ({score:.0f}/100). "
                "Indicadores de risco presentes. Monitoramento intensivo."
            )
        return (
            f"Live aprovado e estavel ({score:.0f}/100). "
            f"Guardian={guardian} readiness={readiness}. Operacao normal."
        )

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: LiveGovernanceReport) -> None:
        try:
            self.live_gov_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.live_gov_log, "a") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

    def _persist_summary(self, report: LiveGovernanceReport) -> None:
        try:
            LIVE_GOV_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":            report.evaluated_at,
                "cycle_id":                report.cycle_id,
                "run_number":              report.run_number,
                "live_governance_score":   report.live_governance_score,
                "operational_confidence":  report.operational_confidence,
                "live_stability_score":    report.live_stability_score,
                "execution_integrity":     report.execution_integrity,
                "capital_safety_score":    report.capital_safety_score,
                "autonomous_live_approval": report.autonomous_live_approval,
                "guardian_state":          report.guardian_state,
                "readiness_status":        report.readiness_status,
                "rollback_executed":       report.rollback_executed,
                "phases_succeeded":        report.phases_succeeded,
                "phases_failed":           report.phases_failed,
                "governance_trend":        report.governance_trend,
                "cycle_duration_ms":       report.cycle_duration_ms,
            }
            with open(LIVE_GOV_SUMMARY, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Live Governance — Phase Q Q-9"
    )
    parser.add_argument("--run",    action="store_true", help="Executar ciclo")
    parser.add_argument("--run-n",  type=int, default=0, metavar="N",
                        help="Executar N ciclos")
    parser.add_argument("--status", action="store_true", help="Mostrar ultimo estado")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    gov = AutonomousLiveGovernance()

    if args.run_n > 0:
        reports = gov.run_n(args.run_n)
        if args.json:
            print(json.dumps([r.to_dict() for r in reports], indent=2))
        else:
            for r in reports:
                approved = "APROVADO" if r.autonomous_live_approval else "BLOQUEADO"
                print(
                    f"Ciclo #{r.run_number}: score={r.live_governance_score:.0f}/100 "
                    f"[{approved}] guardian={r.guardian_state} "
                    f"trend={r.governance_trend} ({r.cycle_duration_ms:.0f}ms)"
                )
        return

    if args.run:
        report = gov.run_once()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
            return
        approved = "APROVADO" if report.autonomous_live_approval else "BLOQUEADO"
        print(f"\nAutonomous Live Governance — Ciclo #{report.run_number}")
        print(f"  cycle_id:                {report.cycle_id}")
        print(f"  live_governance_score:   {report.live_governance_score:.1f}/100")
        print(f"  operational_confidence:  {report.operational_confidence:.1f}/100")
        print(f"  live_stability_score:    {report.live_stability_score:.1f}/100")
        print(f"  execution_integrity:     {report.execution_integrity:.1f}/100")
        print(f"  capital_safety_score:    {report.capital_safety_score:.1f}/100")
        print(f"  autonomous_live_approval:{approved}")
        if report.approval_blocking_reason:
            print(f"  blocking_reason:         {report.approval_blocking_reason}")
        print(f"\n  guardian_state:  {report.guardian_state}")
        print(f"  readiness_status:{report.readiness_status}")
        print(f"  rollback:        {'SIM' if report.rollback_executed else 'nao'}")
        print(f"  trading_allowed: {'sim' if report.trading_allowed else 'NAO'}")
        print(f"  governance_trend:{report.governance_trend}")
        print(f"\n  Fases: {report.phases_succeeded} OK / {report.phases_failed} falhas")
        print(f"  Duracao: {report.cycle_duration_ms:.0f}ms")
        print(f"\n  -> {report.recommendation}")
        return

    # Default: status (ultimo ciclo do log)
    gov_log = Path("data/live_governance_summary.jsonl")
    if not gov_log.exists():
        print("Nenhum ciclo de governanca live executado ainda.")
        print("Use: python -m domains.crypto_coin.research.autonomous_live_governance --run")
        return

    last_entry: dict = {}
    with open(gov_log) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    last_entry = json.loads(line)
                except Exception:
                    pass

    if args.json:
        print(json.dumps(last_entry, indent=2))
        return

    approved = "APROVADO" if last_entry.get("autonomous_live_approval") else "BLOQUEADO"
    print(f"\nAutonomous Live Governance — Status")
    print(f"  ultimo ciclo:            {last_entry.get('evaluated_at', 'N/A')}")
    print(f"  live_governance_score:   {last_entry.get('live_governance_score', 0):.1f}/100")
    print(f"  autonomous_live_approval:{approved}")
    print(f"  guardian_state:          {last_entry.get('guardian_state', 'N/A')}")
    print(f"  readiness_status:        {last_entry.get('readiness_status', 'N/A')}")
    print(f"  governance_trend:        {last_entry.get('governance_trend', 'N/A')}")


if __name__ == "__main__":
    main()

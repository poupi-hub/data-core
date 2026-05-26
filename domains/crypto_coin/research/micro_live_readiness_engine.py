"""
micro_live_readiness_engine.py — Phase P FASE 6

Micro-Live Readiness Engine.

Avalia se o sistema esta pronto para execucao micro-live controlada.
MICRO-LIVE = capital extremamente pequeno, foco em validacao operacional.

Valida:
  - governance_readiness:     sistema de governanca estavel e auditado
  - execution_readiness:      mecanismos de execucao com lineage completo
  - capital_preservation_ready: capital preservation demonstravelmente funcional
  - risk_management_ready:    adaptive risk + safe constraints implementados
  - observability_ready:      Prometheus + logs + auditoria completos
  - stability_validated:      sem runaway, allocation estavel, sem loops

Scores produzidos:
  - live_readiness_score:        prontidao geral para micro-live (0-100)
  - execution_reliability_score: confiabilidade do sistema de execucao (0-100)
  - slippage_quality_score:      qualidade estimada de execucao real (0-100)

CLI:
  python -m domains.crypto_coin.research.micro_live_readiness_engine
  python -m domains.crypto_coin.research.micro_live_readiness_engine --json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

READINESS_LOG = Path("data/live_readiness_log.jsonl")

GOVERNANCE_LOG   = Path("data/governance_history.jsonl")
EXECUTION_LOG    = Path("data/execution_intelligence_log.jsonl")
AUDIT_LOG        = Path("data/behavior_audit_log.jsonl")
STABILITY_LOG    = Path("data/stability_intelligence_log.jsonl")
PRESERVATION_LOG = Path("data/capital_preservation_log.jsonl")
CATASTROP_LOG    = Path("data/catastrophic_simulation_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import live_readiness_score as _prom_readiness
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

MIN_GOVERNANCE_CYCLES    = 3    # minimo de ciclos de governance para aprovar
MIN_EXECUTION_DECISIONS  = 5    # minimo de decisoes de execucao
MIN_AUTONOMY_STABILITY   = 60.0 # stability score minimo
MIN_CAPITAL_SURVIVAL     = 65.0 # capital survival score minimo
MIN_CATASTROPHIC_PASS    = 4    # minimo de cenarios catastroficos passados
REQUIRED_LOG_FILES = [          # arquivos de log que devem existir e nao estar vazios
    GOVERNANCE_LOG,
    EXECUTION_LOG,
]


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ReadinessCheck:
    """Check individual de prontidao."""
    gate:         str
    passed:       bool
    score:        float
    description:  str
    blocking:     bool   # True = bloqueia aprovacao se falhar


@dataclass
class LiveReadinessReport:
    """Relatorio de prontidao para micro-live."""
    live_readiness_score:        float  # 0-100
    execution_reliability_score: float  # 0-100
    slippage_quality_score:      float  # 0-100 (estimativa baseada em historico)

    gates:                       list[ReadinessCheck]
    gates_passed:                int
    gates_failed:                int
    blocking_failures:           int

    approved_for_micro_live:     bool   # True apenas se todos os gates blocking passaram
    approval_conditions:         list[str]  # condicoes pendentes

    # Evidencias
    governance_cycles_logged:    int
    execution_decisions_logged:  int
    autonomy_stability_score:    float
    capital_survival_score:      float
    catastrophic_scenarios_passed: int

    readiness_recommendation:    str
    warning:                     str
    evaluated_at:                str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gates"] = [asdict(g) for g in self.gates]
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class MicroLiveReadinessEngine:
    """
    FASE 6: Avalia prontidao para micro-live controlado.

    Consolida achados de todas as camadas de validacao Phase P.
    Emite aprovacao formal apenas quando todos os gates blocking passam.

    MICRO-LIVE SIGNIFICA:
    - Capital simbolico (ex: $100-500)
    - Objetivo: validar latencia, fills, slippage, fees reais
    - Nao otimizacao de lucro
    """

    def evaluate(self) -> LiveReadinessReport:
        gates: list[ReadinessCheck] = []
        approval_conditions: list[str] = []

        # Carregar evidencias dos modulos anteriores
        gov_records      = self._load_jsonl(GOVERNANCE_LOG)
        exec_records     = self._load_jsonl(EXECUTION_LOG)
        audit_records    = self._load_jsonl(AUDIT_LOG)
        stability_records = self._load_jsonl(STABILITY_LOG)
        preservation_records = self._load_jsonl(PRESERVATION_LOG)
        catastrop_records = self._load_jsonl(CATASTROP_LOG)

        gov_count  = len(gov_records)
        exec_count = len(exec_records)

        # Extrair scores dos ultimos registros
        last_audit     = audit_records[-1] if audit_records else {}
        last_stability = stability_records[-1] if stability_records else {}
        last_pres      = preservation_records[-1] if preservation_records else {}
        catastrop_passed = len([r for r in catastrop_records if r.get("passed") and r.get("scenario_name") != "__summary__"])

        autonomy_stability  = last_stability.get("autonomy_stability_score", 0.0)
        capital_survival    = last_pres.get("capital_survival_score", 0.0)
        runaway_detected    = last_audit.get("runaway_detected", True)  # pessimista se sem dados
        gov_looping         = last_audit.get("governance_looping", True)

        # ── Gate 1: Governance Cycles Suficientes (BLOCKING) ─────────────────
        g1_passed = gov_count >= MIN_GOVERNANCE_CYCLES
        gates.append(ReadinessCheck(
            "governance_cycles", g1_passed,
            min(100.0, gov_count / MIN_GOVERNANCE_CYCLES * 100),
            f"{gov_count}/{MIN_GOVERNANCE_CYCLES} ciclos de governance registrados",
            blocking=True,
        ))
        if not g1_passed:
            approval_conditions.append(f"Executar governance ao menos {MIN_GOVERNANCE_CYCLES} vezes completas")

        # ── Gate 2: Execution Decisions Suficientes (BLOCKING) ────────────────
        g2_passed = exec_count >= MIN_EXECUTION_DECISIONS
        gates.append(ReadinessCheck(
            "execution_decisions", g2_passed,
            min(100.0, exec_count / MIN_EXECUTION_DECISIONS * 100),
            f"{exec_count}/{MIN_EXECUTION_DECISIONS} decisoes de execucao registradas",
            blocking=True,
        ))
        if not g2_passed:
            approval_conditions.append(f"Gerar ao menos {MIN_EXECUTION_DECISIONS} decisoes de execucao")

        # ── Gate 3: Sem Runaway Behavior (BLOCKING) ───────────────────────────
        g3_passed = not runaway_detected
        gates.append(ReadinessCheck(
            "no_runaway_behavior", g3_passed,
            100.0 if g3_passed else 0.0,
            "Runaway behavior nao detectado" if g3_passed else "RUNAWAY DETECTADO — sistema instavel",
            blocking=True,
        ))
        if not g3_passed:
            approval_conditions.append("Resolver runaway behavior antes de micro-live")

        # ── Gate 4: Sem Governance Loop (BLOCKING) ────────────────────────────
        g4_passed = not gov_looping
        gates.append(ReadinessCheck(
            "governance_stable", g4_passed,
            100.0 if g4_passed else 20.0,
            "Governance sem loops detectados" if g4_passed else "GOVERNANCE LOOPING — nao apto para live",
            blocking=True,
        ))
        if not g4_passed:
            approval_conditions.append("Implementar hysteresis no governance antes de micro-live")

        # ── Gate 5: Autonomy Stability Score (BLOCKING) ───────────────────────
        g5_passed = autonomy_stability >= MIN_AUTONOMY_STABILITY
        gates.append(ReadinessCheck(
            "autonomy_stability", g5_passed,
            autonomy_stability,
            f"autonomy_stability_score={autonomy_stability:.0f} (minimo={MIN_AUTONOMY_STABILITY})",
            blocking=True,
        ))
        if not g5_passed:
            approval_conditions.append(f"Melhorar autonomy_stability_score para >= {MIN_AUTONOMY_STABILITY}")

        # ── Gate 6: Capital Preservation Validada (BLOCKING) ──────────────────
        g6_passed = capital_survival >= MIN_CAPITAL_SURVIVAL
        gates.append(ReadinessCheck(
            "capital_preservation_validated", g6_passed,
            capital_survival,
            f"capital_survival_score={capital_survival:.0f} (minimo={MIN_CAPITAL_SURVIVAL})",
            blocking=True,
        ))
        if not g6_passed:
            approval_conditions.append(f"capital_survival_score deve atingir >= {MIN_CAPITAL_SURVIVAL}")

        # ── Gate 7: Cenarios Catastroficos Testados (nao blocking) ───────────
        g7_passed = catastrop_passed >= MIN_CATASTROPHIC_PASS
        gates.append(ReadinessCheck(
            "catastrophic_scenarios", g7_passed,
            min(100.0, catastrop_passed / max(MIN_CATASTROPHIC_PASS, 1) * 100),
            f"{catastrop_passed}/{MIN_CATASTROPHIC_PASS} cenarios catastroficos passados",
            blocking=False,
        ))
        if not g7_passed:
            approval_conditions.append(f"Passar ao menos {MIN_CATASTROPHIC_PASS} cenarios catastroficos")

        # ── Gate 8: Logs de Observabilidade Presentes (nao blocking) ─────────
        obs_ok = all(p.exists() and p.stat().st_size > 0 for p in REQUIRED_LOG_FILES)
        gates.append(ReadinessCheck(
            "observability_logs", obs_ok,
            100.0 if obs_ok else 30.0,
            "Logs de observabilidade presentes e populados" if obs_ok else "Alguns logs ausentes ou vazios",
            blocking=False,
        ))

        # ── Scores ────────────────────────────────────────────────────────────
        gates_passed   = sum(1 for g in gates if g.passed)
        gates_failed   = sum(1 for g in gates if not g.passed)
        blocking_fails = sum(1 for g in gates if not g.passed and g.blocking)

        approved = blocking_fails == 0

        live_readiness = round(gates_passed / max(len(gates), 1) * 100.0, 1)

        # execution_reliability: baseado em exec decisions + sem runaway
        exec_reliability = round(
            min(100.0, exec_count / MIN_EXECUTION_DECISIONS * 50.0)
            + (40.0 if not runaway_detected else 0.0)
            + (10.0 if not gov_looping else 0.0),
            1,
        )

        # slippage_quality: estimativa conservadora (sem dados reais ainda)
        slippage_quality = 60.0  # baseline conservador ate ter dados reais

        recommendation = self._build_recommendation(approved, approval_conditions, blocking_fails)

        report = LiveReadinessReport(
            live_readiness_score         = live_readiness,
            execution_reliability_score  = exec_reliability,
            slippage_quality_score       = slippage_quality,
            gates                        = gates,
            gates_passed                 = gates_passed,
            gates_failed                 = gates_failed,
            blocking_failures            = blocking_fails,
            approved_for_micro_live      = approved,
            approval_conditions          = approval_conditions,
            governance_cycles_logged     = gov_count,
            execution_decisions_logged   = exec_count,
            autonomy_stability_score     = autonomy_stability,
            capital_survival_score       = capital_survival,
            catastrophic_scenarios_passed = catastrop_passed,
            readiness_recommendation     = recommendation,
            warning                      = "PAPER ONLY — micro-live requer aprovacao formal e capital simbolico inicial.",
            evaluated_at                 = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_readiness.set(live_readiness)
            except Exception:
                pass

        return report

    def _build_recommendation(
        self, approved: bool, conditions: list[str], blocking_fails: int
    ) -> str:
        if approved:
            return (
                "APROVADO para micro-live controlado. "
                "Iniciar com capital simbolico ($100-500). "
                "Monitorar slippage, fees e latencia por 72h antes de expandir."
            )
        return (
            f"NAO APROVADO: {blocking_fails} gate(s) blocking falhando. "
            f"Pendencias: {'; '.join(conditions[:3])}."
        )

    def _load_jsonl(self, path: Path, max_records: int = 100) -> list[dict]:
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
        return records[-max_records:]

    def _persist(self, report: LiveReadinessReport) -> None:
        try:
            READINESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":              report.evaluated_at,
                "live_readiness_score":      report.live_readiness_score,
                "execution_reliability_score": report.execution_reliability_score,
                "approved_for_micro_live":   report.approved_for_micro_live,
                "blocking_failures":         report.blocking_failures,
                "gates_passed":              report.gates_passed,
                "gates_failed":              report.gates_failed,
            }
            with open(READINESS_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Micro-Live Readiness Engine — Phase P FASE 6")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = MicroLiveReadinessEngine()
    report = engine.evaluate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    status = "APROVADO" if report.approved_for_micro_live else "NAO APROVADO"
    print(f"\nMicro-Live Readiness Engine  [{status}]")
    print(f"  {report.warning}")
    print(f"\n  live_readiness_score:        {report.live_readiness_score:.0f}/100")
    print(f"  execution_reliability_score: {report.execution_reliability_score:.0f}/100")
    print(f"  slippage_quality_score:      {report.slippage_quality_score:.0f}/100")
    print(f"\n  Gates [{report.gates_passed} passed / {report.gates_failed} failed / {report.blocking_failures} blocking]:")
    for g in report.gates:
        icon    = "OK" if g.passed else ("BLOCK" if g.blocking else "WARN")
        blocker = " [BLOCKING]" if g.blocking and not g.passed else ""
        print(f"    [{icon}] {g.gate:<35} {g.score:>5.0f}{blocker}")
    if report.approval_conditions:
        print(f"\n  Condicoes pendentes:")
        for cond in report.approval_conditions:
            print(f"    - {cond}")
    print(f"\n  -> {report.readiness_recommendation}")


if __name__ == "__main__":
    main()

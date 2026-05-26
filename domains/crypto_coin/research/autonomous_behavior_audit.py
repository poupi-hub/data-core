"""
autonomous_behavior_audit.py — Phase P FASE 1

Autonomous Behavior Audit.

Audita o comportamento emergente do sistema autonomo (Phase O):
  - runaway behavior:       ciclos de decisao auto-amplificados
  - allocation instability: pesos oscilando entre ciclos sem convergir
  - governance loops:       modos de governanca alternando rapidamente
  - exposure drift:         exposure controlada crescendo sem gatilho
  - self-healing spam:      quarentenas excessivas sem melhoria
  - optimization stagnation: meta-optimizer preso em minimo local
  - observability gaps:     metricas faltando ou silenciosas

Scores produzidos:
  - system_autonomy_score:    nivel de autonomia estavel (0-100)
  - runaway_risk_score:       risco de comportamento fora de controle (0-100)
  - operational_stability_score: estabilidade operacional geral (0-100)

CLI:
  python -m domains.crypto_coin.research.autonomous_behavior_audit
  python -m domains.crypto_coin.research.autonomous_behavior_audit --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPERIMENTS_DIR   = Path("data/experiments")
GOVERNANCE_LOG    = Path("data/governance_history.jsonl")
ACTIVATION_LOG    = Path("data/strategy_activation_log.jsonl")
EXECUTION_LOG     = Path("data/execution_intelligence_log.jsonl")
HEALING_LOG       = Path("data/self_healing_log.jsonl")
META_OPT_LOG      = Path("data/meta_optimization_log.jsonl")
AUDIT_LOG         = Path("data/behavior_audit_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import autonomy_stability_score as _prom_autonomy
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

RUNAWAY_SWITCH_RATE      = 0.5    # switches per cycle > 0.5 = instavel
ALLOCATION_CV_THRESH     = 0.40   # CV de allocation > 0.40 = oscilando
GOVERNANCE_MODE_FLIP     = 0.4    # fracao de ciclos com mode-flip > 40% = instavel
HEALING_SPAM_RATE        = 3.0    # issues por ciclo > 3 = self-healing spam
OBS_SILENCE_THRESH       = 10     # < 10 ciclos registrados = observability gap
EXPOSURE_DRIFT_THRESH    = 0.30   # exposure aumentando > 30% sem gatilho = drift


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class AuditFinding:
    """Achado de auditoria de comportamento."""
    finding_id:   str
    finding_type: str   # runaway | allocation_instability | governance_loop | exposure_drift | healing_spam | obs_gap | stagnation
    severity:     str   # low | medium | high | critical
    score:        float # 0-100 (magnitude do problema)
    description:  str
    evidence:     dict  # dados quantitativos que suportam o achado
    recommendation: str


@dataclass
class BehaviorAuditReport:
    """Relatorio de auditoria de comportamento autonomo."""
    system_autonomy_score:       float  # 0-100
    runaway_risk_score:          float  # 0-100
    operational_stability_score: float  # 0-100

    # Componentes auditados
    governance_cycles_analyzed:  int
    activation_events_analyzed:  int
    execution_decisions_analyzed: int
    healing_cycles_analyzed:     int

    findings:                    list[AuditFinding]
    findings_critical:           int
    findings_high:               int

    # Checks individuais
    runaway_detected:            bool
    allocation_unstable:         bool
    governance_looping:          bool
    exposure_drifting:           bool
    healing_spamming:            bool
    observability_gaps:          list[str]

    audit_recommendation:        str
    audited_at:                  str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["findings"] = [asdict(f) for f in self.findings]
        return d


# ── Auditor ────────────────────────────────────────────────────────────────────

class AutonomousBehaviorAuditor:
    """
    FASE 1: Audita comportamento emergente do sistema Phase O.

    Le logs JSONL existentes e detecta padroes problematicos sem
    re-executar nenhum modulo. Auditoria e read-only.
    """

    def audit(self) -> BehaviorAuditReport:
        findings: list[AuditFinding] = []
        obs_gaps: list[str] = []

        # ── Carregar historicos ────────────────────────────────────────────────
        gov_cycles    = self._load_jsonl(GOVERNANCE_LOG)
        act_events    = self._load_jsonl(ACTIVATION_LOG)
        exec_decisions = self._load_jsonl(EXECUTION_LOG)
        heal_cycles   = self._load_jsonl(HEALING_LOG)
        meta_cycles   = self._load_jsonl(META_OPT_LOG)

        # ── Observability gaps ────────────────────────────────────────────────
        for label, path, records in [
            ("governance", GOVERNANCE_LOG, gov_cycles),
            ("activation", ACTIVATION_LOG, act_events),
            ("execution",  EXECUTION_LOG, exec_decisions),
            ("self_healing", HEALING_LOG, heal_cycles),
            ("meta_optimization", META_OPT_LOG, meta_cycles),
        ]:
            if len(records) < OBS_SILENCE_THRESH:
                obs_gaps.append(f"{label} ({len(records)} registros)")
                if len(records) == 0:
                    findings.append(AuditFinding(
                        finding_id    = f"OBS-{label[:3].upper()}",
                        finding_type  = "obs_gap",
                        severity      = "medium",
                        score         = 60.0,
                        description   = f"Log '{label}' vazio — modulo nunca executado",
                        evidence      = {"log_path": str(path), "records": 0},
                        recommendation= f"Executar {label} ao menos uma vez para popular baseline",
                    ))

        # ── 1. Governance Loop Detection ──────────────────────────────────────
        governance_looping = False
        if len(gov_cycles) >= 3:
            modes = [c.get("fleet_control_mode", c.get("governance_mode", "normal")) for c in gov_cycles[-20:]]
            if len(modes) >= 3:
                flips = sum(1 for i in range(1, len(modes)) if modes[i] != modes[i-1])
                flip_rate = flips / len(modes)
                if flip_rate > GOVERNANCE_MODE_FLIP:
                    governance_looping = True
                    findings.append(AuditFinding(
                        finding_id    = "GOV-LOOP-001",
                        finding_type  = "governance_loop",
                        severity      = "high",
                        score         = min(100.0, flip_rate * 150.0),
                        description   = f"Governance mode alternando em {flip_rate:.0%} dos ciclos — instabilidade",
                        evidence      = {"flip_rate": round(flip_rate, 3), "modes_sample": modes[-5:]},
                        recommendation= "Adicionar hysteresis no _determine_control_mode — evitar transicoes rapidas",
                    ))

        # ── 2. Strategy Switch Rate (runaway) ─────────────────────────────────
        runaway_detected = False
        if act_events and gov_cycles:
            switches_total = len([e for e in act_events if e.get("from_state") != e.get("to_state")])
            cycles_total   = max(len(gov_cycles), 1)
            switch_rate    = switches_total / cycles_total
            if switch_rate > RUNAWAY_SWITCH_RATE:
                runaway_detected = True
                findings.append(AuditFinding(
                    finding_id    = "RUN-001",
                    finding_type  = "runaway",
                    severity      = "high",
                    score         = min(100.0, switch_rate * 80.0),
                    description   = f"Taxa de switch={switch_rate:.2f} por ciclo — acima do limiar {RUNAWAY_SWITCH_RATE}",
                    evidence      = {"switches_total": switches_total, "cycles_total": cycles_total, "rate": round(switch_rate, 3)},
                    recommendation= "Revisar thresholds de freeze/throttle — possivelmente FREEZE_RISK_THRESH muito baixo",
                ))

        # ── 3. Allocation Stability ───────────────────────────────────────────
        allocation_unstable = False
        if len(exec_decisions) >= 5:
            by_strategy: dict[str, list[float]] = {}
            for d in exec_decisions[-50:]:
                sid    = d.get("strategy_id", "")
                sizing = d.get("final_sizing", 0.0)
                by_strategy.setdefault(sid, []).append(sizing)

            unstable_strategies: list[str] = []
            for sid, sizings in by_strategy.items():
                if len(sizings) >= 3:
                    try:
                        mean = statistics.mean(sizings)
                        std  = statistics.stdev(sizings)
                        cv   = std / mean if mean > 0 else 0.0
                        if cv > ALLOCATION_CV_THRESH:
                            unstable_strategies.append(sid)
                    except statistics.StatisticsError:
                        pass

            if unstable_strategies:
                allocation_unstable = True
                findings.append(AuditFinding(
                    finding_id    = "ALLOC-UNSTABLE-001",
                    finding_type  = "allocation_instability",
                    severity      = "medium",
                    score         = min(100.0, len(unstable_strategies) / max(len(by_strategy), 1) * 100),
                    description   = f"{len(unstable_strategies)} estrategia(s) com sizing oscilando: {unstable_strategies[:3]}",
                    evidence      = {"unstable_strategies": unstable_strategies, "cv_threshold": ALLOCATION_CV_THRESH},
                    recommendation= "Adicionar smoothing no allocation_weight — media exponencial entre ciclos",
                ))

        # ── 4. Exposure Drift ─────────────────────────────────────────────────
        exposure_drifting = False
        if len(exec_decisions) >= 10:
            recent_exp  = [d.get("controlled_exposure", 0.0) for d in exec_decisions[-5:]]
            earlier_exp = [d.get("controlled_exposure", 0.0) for d in exec_decisions[-10:-5]]
            if recent_exp and earlier_exp:
                avg_recent  = statistics.mean(recent_exp)
                avg_earlier = statistics.mean(earlier_exp)
                if avg_earlier > 0 and (avg_recent - avg_earlier) / avg_earlier > EXPOSURE_DRIFT_THRESH:
                    exposure_drifting = True
                    findings.append(AuditFinding(
                        finding_id    = "EXP-DRIFT-001",
                        finding_type  = "exposure_drift",
                        severity      = "medium",
                        score         = min(100.0, ((avg_recent - avg_earlier) / avg_earlier) * 100),
                        description   = f"Exposure crescendo: {avg_earlier:.2f} → {avg_recent:.2f} sem evento de mercado claro",
                        evidence      = {"avg_earlier": round(avg_earlier, 3), "avg_recent": round(avg_recent, 3)},
                        recommendation= "Verificar se capital_preservation_active esta sendo calculado corretamente",
                    ))

        # ── 5. Self-Healing Spam ──────────────────────────────────────────────
        healing_spamming = False
        if len(heal_cycles) >= 3:
            recent_issues = [h.get("issues_count", 0) for h in heal_cycles[-10:]]
            avg_issues    = statistics.mean(recent_issues) if recent_issues else 0
            if avg_issues > HEALING_SPAM_RATE:
                healing_spamming = True
                findings.append(AuditFinding(
                    finding_id    = "HEAL-SPAM-001",
                    finding_type  = "healing_spam",
                    severity      = "medium",
                    score         = min(100.0, avg_issues * 20.0),
                    description   = f"Self-healing reportando {avg_issues:.1f} issues por ciclo — possivel spam ou dataset corrompido",
                    evidence      = {"avg_issues_per_cycle": round(avg_issues, 2), "threshold": HEALING_SPAM_RATE},
                    recommendation= "Revisar dados em data/experiments/ — checar JSONL corrompidos e datasets inconsistentes",
                ))

        # ── 6. Meta-Optimization Stagnation ───────────────────────────────────
        if len(meta_cycles) >= 5:
            stagnation_counts = [m.get("strategies_stagnant", 0) for m in meta_cycles[-10:]]
            avg_stagnant = statistics.mean(stagnation_counts) if stagnation_counts else 0
            if avg_stagnant >= 2:
                findings.append(AuditFinding(
                    finding_id    = "META-STAG-001",
                    finding_type  = "stagnation",
                    severity      = "low",
                    score         = min(100.0, avg_stagnant * 25.0),
                    description   = f"Media de {avg_stagnant:.1f} estrategias estagnadas por ciclo de otimizacao",
                    evidence      = {"avg_stagnant": round(avg_stagnant, 2)},
                    recommendation= "Expandir espaco de parametros ou usar Bayesian sweep nas estrategias estagnadas",
                ))

        # ── Compute scores ─────────────────────────────────────────────────────
        critical_count = sum(1 for f in findings if f.severity == "critical")
        high_count     = sum(1 for f in findings if f.severity == "high")

        runaway_risk = min(100.0,
            (50.0 if runaway_detected else 0.0) +
            (30.0 if governance_looping else 0.0) +
            (20.0 if allocation_unstable else 0.0)
        )

        operational_stability = max(0.0,
            100.0
            - critical_count * 30.0
            - high_count     * 15.0
            - len(obs_gaps)  * 8.0
            - (10.0 if healing_spamming else 0.0)
        )

        system_autonomy = max(0.0, min(100.0,
            (100.0 - runaway_risk * 0.4)
            * (operational_stability / 100.0)
        ))

        recommendation = self._build_recommendation(
            runaway_detected, governance_looping, allocation_unstable, obs_gaps
        )

        report = BehaviorAuditReport(
            system_autonomy_score        = round(system_autonomy, 1),
            runaway_risk_score           = round(runaway_risk, 1),
            operational_stability_score  = round(operational_stability, 1),
            governance_cycles_analyzed   = len(gov_cycles),
            activation_events_analyzed   = len(act_events),
            execution_decisions_analyzed = len(exec_decisions),
            healing_cycles_analyzed      = len(heal_cycles),
            findings                     = findings,
            findings_critical            = critical_count,
            findings_high                = high_count,
            runaway_detected             = runaway_detected,
            allocation_unstable          = allocation_unstable,
            governance_looping           = governance_looping,
            exposure_drifting            = exposure_drifting,
            healing_spamming             = healing_spamming,
            observability_gaps           = obs_gaps,
            audit_recommendation         = recommendation,
            audited_at                   = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_autonomy.set(system_autonomy)
            except Exception:
                pass

        return report

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _load_jsonl(self, path: Path, max_records: int = 200) -> list[dict]:
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
        return records[-max_records:]  # ultimos N registros

    def _build_recommendation(
        self,
        runaway:   bool,
        gov_loop:  bool,
        alloc_unstable: bool,
        obs_gaps:  list[str],
    ) -> str:
        if runaway and gov_loop:
            return "CRITICO: runaway + governance loop simultaneos. Revisar thresholds de controle imediatamente."
        if runaway:
            return "Runaway behavior detectado. Adicionar hysteresis e rate-limiting nas transicoes de estado."
        if gov_loop:
            return "Governance looping. Implementar cooldown minimo entre mudancas de control_mode."
        if alloc_unstable:
            return "Allocation oscilando. Adicionar smoothing (EMA) no calculo de allocation_weight."
        if obs_gaps:
            return f"Gaps de observabilidade: {obs_gaps}. Executar ciclo completo de governance para popular logs."
        return "Sistema autonomo sem problemas criticos detectados. Comportamento dentro dos parametros."

    def _persist(self, report: BehaviorAuditReport) -> None:
        try:
            AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "audited_at":                  report.audited_at,
                "system_autonomy_score":       report.system_autonomy_score,
                "runaway_risk_score":          report.runaway_risk_score,
                "operational_stability_score": report.operational_stability_score,
                "findings_count":              len(report.findings),
                "findings_critical":           report.findings_critical,
                "runaway_detected":            report.runaway_detected,
                "governance_looping":          report.governance_looping,
                "observability_gaps":          report.observability_gaps,
            }
            with open(AUDIT_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Behavior Audit — Phase P FASE 1")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    auditor = AutonomousBehaviorAuditor()
    report  = auditor.audit()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nAutonomous Behavior Audit")
    print(f"  system_autonomy_score:       {report.system_autonomy_score:.0f}/100")
    print(f"  runaway_risk_score:          {report.runaway_risk_score:.0f}/100")
    print(f"  operational_stability_score: {report.operational_stability_score:.0f}/100")
    print(f"\n  Historico analisado:")
    print(f"    governance_cycles:    {report.governance_cycles_analyzed}")
    print(f"    activation_events:    {report.activation_events_analyzed}")
    print(f"    execution_decisions:  {report.execution_decisions_analyzed}")
    print(f"    healing_cycles:       {report.healing_cycles_analyzed}")
    print(f"\n  Checks:")
    print(f"    runaway_detected:     {'SIM' if report.runaway_detected else 'nao'}")
    print(f"    allocation_unstable:  {'SIM' if report.allocation_unstable else 'nao'}")
    print(f"    governance_looping:   {'SIM' if report.governance_looping else 'nao'}")
    print(f"    exposure_drifting:    {'SIM' if report.exposure_drifting else 'nao'}")
    print(f"    healing_spamming:     {'SIM' if report.healing_spamming else 'nao'}")
    if report.observability_gaps:
        print(f"    obs_gaps:             {report.observability_gaps}")
    if report.findings:
        print(f"\n  Achados ({len(report.findings)}):")
        for f in report.findings:
            print(f"    [{f.severity.upper()}] {f.finding_type}: {f.description}")
    print(f"\n  -> {report.audit_recommendation}")


if __name__ == "__main__":
    main()

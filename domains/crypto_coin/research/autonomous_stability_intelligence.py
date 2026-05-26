"""
autonomous_stability_intelligence.py — Phase P FASE 3

Autonomous Stability Intelligence.

Detecta instabilidade no comportamento adaptativo do sistema:
  - oscillating_allocation:    pesos mudando mais de X% entre ciclos
  - unstable_exposure:         exposure variando sem gatilho de mercado
  - excessive_switching:       estrategias mudando de estado repetidamente
  - governance_instability:    governance_mode alternando sem convergencia
  - recursive_degradation:     estrategia degradando → nova degradacao → loop
  - runaway_optimization:      meta-optimizer ampliando problemas existentes

Scores produzidos:
  - autonomy_stability_score:      estabilidade do comportamento autonomo (0-100)
  - allocation_stability_score:    estabilidade da alocacao (0-100)
  - governance_consistency_score:  consistencia das decisoes de governanca (0-100)

CLI:
  python -m domains.crypto_coin.research.autonomous_stability_intelligence
  python -m domains.crypto_coin.research.autonomous_stability_intelligence --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

EXECUTION_LOG    = Path("data/execution_intelligence_log.jsonl")
GOVERNANCE_LOG   = Path("data/governance_history.jsonl")
ACTIVATION_LOG   = Path("data/strategy_activation_log.jsonl")
EXPOSURE_LOG     = Path("data/exposure_control_log.jsonl")
STABILITY_LOG    = Path("data/stability_intelligence_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import autonomy_stability_score as _prom_stability
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

MIN_CYCLES_FOR_STABILITY  = 5    # minimo de ciclos para avaliar estabilidade
ALLOC_OSCILLATION_THRESH  = 0.35 # CV de sizing > 0.35 = oscilando
EXPOSURE_CV_THRESH        = 0.30 # CV de exposure > 0.30 = instavel
SWITCH_RATE_THRESH        = 0.40 # switches por ciclo > 0.40 = excessivo
GOV_FLIP_THRESH           = 0.35 # fracao de flips entre ciclos consecutivos
RECURSIVE_DEGRAD_WINDOW   = 5    # window de ciclos para detectar recursao


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class StabilitySignal:
    signal_type: str
    severity:    str   # low | medium | high | critical
    score:       float
    strategy_id: str | None
    description: str
    evidence:    dict


@dataclass
class StabilityReport:
    """Relatorio de estabilidade do comportamento autonomo."""
    autonomy_stability_score:     float  # 0-100
    allocation_stability_score:   float  # 0-100
    governance_consistency_score: float  # 0-100

    # Checks
    allocation_oscillating:  bool
    exposure_unstable:       bool
    switching_excessive:     bool
    governance_inconsistent: bool
    recursive_degradation:   bool

    # Evidencias
    strategies_oscillating:  list[str]
    governance_flip_rate:    float
    avg_switch_rate:         float

    signals:                 list[StabilitySignal]
    stability_recommendation: str
    evaluated_at:            str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = [asdict(s) for s in self.signals]
        return d


# ── Analyzer ───────────────────────────────────────────────────────────────────

class AutonomousStabilityIntelligence:
    """
    FASE 3: Detecta instabilidade no comportamento adaptativo emergente.

    Le historicos de execucao, governance e activation para identificar
    padroes de oscilacao, loops e instabilidade estrutural.
    """

    def analyze(self) -> StabilityReport:
        signals: list[StabilitySignal] = []
        strategies_oscillating: list[str] = []

        exec_records = self._load_jsonl(EXECUTION_LOG, 100)
        gov_records  = self._load_jsonl(GOVERNANCE_LOG, 50)
        act_records  = self._load_jsonl(ACTIVATION_LOG, 100)
        exp_records  = self._load_jsonl(EXPOSURE_LOG, 100)

        # ── 1. Allocation Oscillation ──────────────────────────────────────────
        allocation_oscillating = False
        if len(exec_records) >= MIN_CYCLES_FOR_STABILITY:
            by_strategy: dict[str, list[float]] = {}
            for r in exec_records:
                sid   = r.get("strategy_id", "")
                siz   = r.get("final_sizing", 0.0)
                by_strategy.setdefault(sid, []).append(siz)

            alloc_cvs: list[float] = []
            for sid, sizings in by_strategy.items():
                if len(sizings) >= 3:
                    try:
                        mean = statistics.mean(sizings)
                        std  = statistics.stdev(sizings)
                        cv   = std / mean if mean > 0 else 0.0
                        alloc_cvs.append(cv)
                        if cv > ALLOC_OSCILLATION_THRESH:
                            strategies_oscillating.append(sid)
                            signals.append(StabilitySignal(
                                "oscillating_allocation", "medium",
                                min(100.0, cv * 150.0), sid,
                                f"Sizing oscilando: CV={cv:.2f} (>{ALLOC_OSCILLATION_THRESH})",
                                {"cv": round(cv, 3), "sizings_sample": sizings[-4:]},
                            ))
                    except statistics.StatisticsError:
                        pass

            if strategies_oscillating:
                allocation_oscillating = True

            alloc_stability = max(0.0, 100.0 - statistics.mean(alloc_cvs) * 200.0) if alloc_cvs else 100.0
        else:
            alloc_stability = 50.0  # sem dados suficientes

        # ── 2. Exposure Instability ────────────────────────────────────────────
        exposure_unstable = False
        if len(exp_records) >= MIN_CYCLES_FOR_STABILITY:
            exposures = [r.get("controlled_exposure", 0.0) for r in exp_records]
            try:
                mean_exp = statistics.mean(exposures)
                std_exp  = statistics.stdev(exposures)
                cv_exp   = std_exp / mean_exp if mean_exp > 0 else 0.0
                if cv_exp > EXPOSURE_CV_THRESH:
                    exposure_unstable = True
                    signals.append(StabilitySignal(
                        "unstable_exposure", "medium",
                        min(100.0, cv_exp * 200.0), None,
                        f"Exposure controlada instavel: CV={cv_exp:.2f} (>{EXPOSURE_CV_THRESH})",
                        {"cv": round(cv_exp, 3), "mean": round(mean_exp, 3), "std": round(std_exp, 3)},
                    ))
            except statistics.StatisticsError:
                pass

        # ── 3. Excessive Switching ─────────────────────────────────────────────
        switching_excessive = False
        avg_switch_rate = 0.0
        if act_records and gov_records:
            total_switches = len([r for r in act_records if r.get("from_state") != r.get("to_state")])
            total_cycles   = max(len(gov_records), 1)
            avg_switch_rate = total_switches / total_cycles
            if avg_switch_rate > SWITCH_RATE_THRESH:
                switching_excessive = True
                signals.append(StabilitySignal(
                    "excessive_switching", "high",
                    min(100.0, avg_switch_rate * 100.0), None,
                    f"Taxa de switches={avg_switch_rate:.2f}/ciclo excede {SWITCH_RATE_THRESH}",
                    {"total_switches": total_switches, "total_cycles": total_cycles, "rate": round(avg_switch_rate, 3)},
                ))

        # ── 4. Governance Inconsistency ────────────────────────────────────────
        governance_inconsistent = False
        gov_flip_rate = 0.0
        if len(gov_records) >= MIN_CYCLES_FOR_STABILITY:
            modes = [
                r.get("fleet_control_mode", r.get("governance_mode", "normal"))
                for r in gov_records
            ]
            flips = sum(1 for i in range(1, len(modes)) if modes[i] != modes[i-1])
            gov_flip_rate = flips / max(len(modes) - 1, 1)
            if gov_flip_rate > GOV_FLIP_THRESH:
                governance_inconsistent = True
                signals.append(StabilitySignal(
                    "governance_instability", "high",
                    min(100.0, gov_flip_rate * 150.0), None,
                    f"Governance mode flip rate={gov_flip_rate:.0%} — sistema oscilando entre modos",
                    {"flip_rate": round(gov_flip_rate, 3), "modes_sample": modes[-6:]},
                ))

        # ── 5. Recursive Degradation ───────────────────────────────────────────
        recursive_degradation = False
        if act_records:
            freeze_events = [r for r in act_records if r.get("to_state") == "frozen"]
            by_sid_freezes: dict[str, int] = {}
            for ev in freeze_events:
                sid = ev.get("strategy_id", "")
                by_sid_freezes[sid] = by_sid_freezes.get(sid, 0) + 1

            repeatedly_frozen = [sid for sid, cnt in by_sid_freezes.items() if cnt >= 3]
            if repeatedly_frozen:
                recursive_degradation = True
                signals.append(StabilitySignal(
                    "recursive_degradation", "high",
                    min(100.0, len(repeatedly_frozen) * 30.0), None,
                    f"{len(repeatedly_frozen)} estrategia(s) congeladas 3+ vezes: {repeatedly_frozen[:3]}",
                    {"repeatedly_frozen": repeatedly_frozen, "freeze_counts": by_sid_freezes},
                ))

        # ── Compute composite scores ───────────────────────────────────────────
        gov_consistency = max(0.0, 100.0 - gov_flip_rate * 200.0)

        instability_penalty = (
            (20.0 if allocation_oscillating else 0.0) +
            (15.0 if exposure_unstable else 0.0) +
            (25.0 if switching_excessive else 0.0) +
            (25.0 if governance_inconsistent else 0.0) +
            (15.0 if recursive_degradation else 0.0)
        )
        autonomy_stability = max(0.0, 100.0 - instability_penalty)

        recommendation = self._build_recommendation(
            allocation_oscillating, governance_inconsistent,
            switching_excessive, recursive_degradation,
        )

        report = StabilityReport(
            autonomy_stability_score     = round(autonomy_stability, 1),
            allocation_stability_score   = round(alloc_stability, 1),
            governance_consistency_score = round(gov_consistency, 1),
            allocation_oscillating       = allocation_oscillating,
            exposure_unstable            = exposure_unstable,
            switching_excessive          = switching_excessive,
            governance_inconsistent      = governance_inconsistent,
            recursive_degradation        = recursive_degradation,
            strategies_oscillating       = strategies_oscillating,
            governance_flip_rate         = round(gov_flip_rate, 3),
            avg_switch_rate              = round(avg_switch_rate, 3),
            signals                      = signals,
            stability_recommendation     = recommendation,
            evaluated_at                 = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_stability.set(autonomy_stability)
            except Exception:
                pass

        return report

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

    def _build_recommendation(
        self, osc: bool, gov: bool, switch: bool, recur: bool
    ) -> str:
        if switch and gov:
            return "Switching excessivo + governance instavel. Implementar cooldown minimo de 2 ciclos entre transicoes."
        if recur:
            return "Degradacao recursiva detectada. Aumentar RECOVERY_HEALTH_THRESHOLD e adicionar quarentena temporizada."
        if osc:
            return "Allocation oscilando. Adicionar EMA (alpha=0.3) ao calculo de allocation_weight entre ciclos consecutivos."
        if gov:
            return "Governance inconsistente. Adicionar hysteresis: exigir 2 ciclos consecutivos no mesmo estado antes de transicao."
        return "Estabilidade dentro dos parametros. Continuar monitoramento."

    def _persist(self, report: StabilityReport) -> None:
        try:
            STABILITY_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":               report.evaluated_at,
                "autonomy_stability_score":    report.autonomy_stability_score,
                "allocation_stability_score":  report.allocation_stability_score,
                "governance_consistency_score": report.governance_consistency_score,
                "allocation_oscillating":      report.allocation_oscillating,
                "governance_inconsistent":     report.governance_inconsistent,
                "switching_excessive":         report.switching_excessive,
            }
            with open(STABILITY_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Stability Intelligence — Phase P FASE 3")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    analyzer = AutonomousStabilityIntelligence()
    report   = analyzer.analyze()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nAutonomous Stability Intelligence")
    print(f"  autonomy_stability_score:     {report.autonomy_stability_score:.0f}/100")
    print(f"  allocation_stability_score:   {report.allocation_stability_score:.0f}/100")
    print(f"  governance_consistency_score: {report.governance_consistency_score:.0f}/100")
    print(f"\n  Checks:")
    print(f"    allocation_oscillating:  {'SIM' if report.allocation_oscillating else 'nao'}")
    print(f"    exposure_unstable:       {'SIM' if report.exposure_unstable else 'nao'}")
    print(f"    switching_excessive:     {'SIM' if report.switching_excessive else 'nao'} (rate={report.avg_switch_rate:.2f}/ciclo)")
    print(f"    governance_inconsistent: {'SIM' if report.governance_inconsistent else 'nao'} (flip_rate={report.governance_flip_rate:.0%})")
    print(f"    recursive_degradation:   {'SIM' if report.recursive_degradation else 'nao'}")
    if report.strategies_oscillating:
        print(f"    strategies_oscillating:  {report.strategies_oscillating}")
    if report.signals:
        print(f"\n  Sinais ({len(report.signals)}):")
        for s in report.signals:
            sid_str = f" [{s.strategy_id}]" if s.strategy_id else ""
            print(f"    [{s.severity.upper()}] {s.signal_type}{sid_str}: {s.description}")
    print(f"\n  -> {report.stability_recommendation}")


if __name__ == "__main__":
    main()

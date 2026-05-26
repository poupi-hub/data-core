"""
governance_drift_intelligence.py — Phase P FASE 9

Governance Drift Intelligence.

O sistema autonomo pode degradar sua propria qualidade de governanca:
  - overreaction:         sistema reagindo de forma exagerada a sinais fracos
  - underreaction:        sistema ignorando sinais claros de risco
  - excessive_adaptation: mudando parametros/estados com frequencia excessiva
  - delayed_adaptation:   lento para reagir a condicoes de mudanca rapida
  - unstable_governance:  modos de controle oscilando sem convergir

Scores produzidos:
  - governance_drift_score:    deriva da qualidade de governanca (0-100, 0=sem deriva)
  - adaptation_quality_score:  qualidade da adaptacao autonoma (0-100)
  - autonomous_balance_score:  equilibrio entre sobre/subreacao (0-100)

CLI:
  python -m domains.crypto_coin.research.governance_drift_intelligence
  python -m domains.crypto_coin.research.governance_drift_intelligence --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

GOVERNANCE_LOG   = Path("data/governance_history.jsonl")
SURVIVAL_LOG     = Path("data/survival_history.jsonl")
EXPOSURE_LOG     = Path("data/exposure_control_log.jsonl")
ACTIVATION_LOG   = Path("data/strategy_activation_log.jsonl")
GOV_DRIFT_LOG    = Path("data/governance_drift_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import governance_drift_score as _prom_gov_drift
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

OVERREACTION_EXPOSURE_DROP = 0.50  # exposure caiu > 50% sem justificativa (drift < 40)
UNDERREACTION_RISK_THRESH  = 0.70  # risk >= 70% sem mudanca de mode = underreaction
ADAPTATION_SWITCH_RATE     = 0.50  # > 0.5 switches/ciclo = adaptation excessiva
DELAY_RISK_WINDOW          = 3     # risk alto por 3+ ciclos sem reacao = delay
MODE_STABILITY_WINDOW      = 5     # janela para avaliar estabilidade de governance mode


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class GovernanceDriftSignal:
    signal_type:  str
    severity:     str
    score:        float
    description:  str
    evidence:     dict


@dataclass
class GovernanceDriftReport:
    """Relatorio de deriva de qualidade de governanca."""
    governance_drift_score:   float   # 0-100 (0=sem deriva, 100=governanca degradada)
    adaptation_quality_score: float   # 0-100
    autonomous_balance_score: float   # 0-100

    # Checks individuais
    overreaction_detected:    bool
    underreaction_detected:   bool
    excessive_adaptation:     bool
    delayed_adaptation:       bool
    governance_oscillating:   bool

    signals:                  list[GovernanceDriftSignal]
    cycles_analyzed:          int

    drift_recommendation:     str
    evaluated_at:             str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = [asdict(s) for s in self.signals]
        return d


# ── Analyzer ───────────────────────────────────────────────────────────────────

class GovernanceDriftIntelligence:
    """
    FASE 9: Detecta deriva na qualidade do proprio sistema de governanca.

    Analisa historico de governance para identificar padroes sistematicos
    de sobre/subreacao, delay e oscilacao.
    """

    def analyze(self) -> GovernanceDriftReport:
        signals: list[GovernanceDriftSignal] = []

        gov_records  = self._load_jsonl(GOVERNANCE_LOG, 50)
        exp_records  = self._load_jsonl(EXPOSURE_LOG, 100)
        act_records  = self._load_jsonl(ACTIVATION_LOG, 100)

        n = len(gov_records)
        if n < 3:
            return self._minimal_report(n)

        modes       = [r.get("fleet_control_mode", r.get("governance_mode", "normal")) for r in gov_records]
        drifts      = [r.get("market_drift_score", 0.0) for r in gov_records]
        sys_risks   = [r.get("systemic_risk_score", 0.0) for r in gov_records]
        survivals   = [r.get("market_survival_score", 100.0) for r in gov_records]

        # ── 1. Overreaction Detection ─────────────────────────────────────────
        overreaction_detected = False
        if exp_records:
            low_drift_emergency = [
                r for r in exp_records
                if r.get("market_drift_score", 0.0) < 40.0
                and r.get("fleet_control_mode") in ("emergency", "survival")
            ]
            if len(low_drift_emergency) >= 2:
                overreaction_detected = True
                signals.append(GovernanceDriftSignal(
                    "overreaction", "high",
                    min(100.0, len(low_drift_emergency) * 25.0),
                    f"Emergency/survival mode ativado {len(low_drift_emergency)}x com drift < 40",
                    {"low_drift_emergency_count": len(low_drift_emergency)},
                ))

        # ── 2. Underreaction Detection ────────────────────────────────────────
        underreaction_detected = False
        high_risk_normal = [
            (d, m, sr) for d, m, sr in zip(drifts, modes, sys_risks)
            if sr >= UNDERREACTION_RISK_THRESH * 100 and m == "normal"
        ]
        if len(high_risk_normal) >= 2:
            underreaction_detected = True
            signals.append(GovernanceDriftSignal(
                "underreaction", "high",
                min(100.0, len(high_risk_normal) * 30.0),
                f"Mode=normal com systemic_risk alto em {len(high_risk_normal)} ciclos",
                {"episodes": len(high_risk_normal), "threshold": UNDERREACTION_RISK_THRESH},
            ))

        # ── 3. Excessive Adaptation ───────────────────────────────────────────
        excessive_adaptation = False
        if act_records and n > 0:
            total_switches = len([r for r in act_records if r.get("from_state") != r.get("to_state")])
            switch_rate    = total_switches / n
            if switch_rate > ADAPTATION_SWITCH_RATE:
                excessive_adaptation = True
                signals.append(GovernanceDriftSignal(
                    "excessive_adaptation", "medium",
                    min(100.0, switch_rate * 100.0),
                    f"Taxa de adaptacao excessiva: {switch_rate:.2f} switches/ciclo",
                    {"switch_rate": round(switch_rate, 3), "threshold": ADAPTATION_SWITCH_RATE},
                ))

        # ── 4. Delayed Adaptation ─────────────────────────────────────────────
        delayed_adaptation = False
        consecutive_high_risk = 0
        max_consecutive = 0
        for sr, mode in zip(sys_risks, modes):
            if sr >= 65.0 and mode == "normal":
                consecutive_high_risk += 1
                max_consecutive = max(max_consecutive, consecutive_high_risk)
            else:
                consecutive_high_risk = 0

        if max_consecutive >= DELAY_RISK_WINDOW:
            delayed_adaptation = True
            signals.append(GovernanceDriftSignal(
                "delayed_adaptation", "high",
                min(100.0, max_consecutive * 20.0),
                f"Risco alto por {max_consecutive} ciclos consecutivos sem resposta de emergencia",
                {"max_consecutive": max_consecutive, "window": DELAY_RISK_WINDOW},
            ))

        # ── 5. Governance Oscillation ─────────────────────────────────────────
        governance_oscillating = False
        if len(modes) >= MODE_STABILITY_WINDOW:
            window = modes[-MODE_STABILITY_WINDOW:]
            flips  = sum(1 for i in range(1, len(window)) if window[i] != window[i-1])
            flip_rate = flips / max(len(window) - 1, 1)
            if flip_rate > 0.60:
                governance_oscillating = True
                signals.append(GovernanceDriftSignal(
                    "governance_oscillating", "medium",
                    min(100.0, flip_rate * 120.0),
                    f"Governance mode oscilando: {flip_rate:.0%} de flip rate na ultima janela",
                    {"flip_rate": round(flip_rate, 3), "window": window},
                ))

        # ── Compute scores ─────────────────────────────────────────────────────
        drift_components = [
            30.0 if overreaction_detected else 0.0,
            30.0 if underreaction_detected else 0.0,
            20.0 if excessive_adaptation else 0.0,
            25.0 if delayed_adaptation else 0.0,
            15.0 if governance_oscillating else 0.0,
        ]
        governance_drift = min(100.0, sum(drift_components))

        # Adaptation quality: menos switches + menos delay = melhor
        adaptation_penalty = (
            (30.0 if delayed_adaptation else 0.0) +
            (20.0 if excessive_adaptation else 0.0) +
            (10.0 if governance_oscillating else 0.0)
        )
        adaptation_quality = max(0.0, 100.0 - adaptation_penalty)

        # Autonomous balance: sem over nem under
        balance_penalty = (
            (35.0 if overreaction_detected else 0.0) +
            (35.0 if underreaction_detected else 0.0)
        )
        autonomous_balance = max(0.0, 100.0 - balance_penalty)

        recommendation = self._build_recommendation(
            overreaction_detected, underreaction_detected,
            delayed_adaptation, governance_oscillating,
        )

        report = GovernanceDriftReport(
            governance_drift_score   = round(governance_drift, 1),
            adaptation_quality_score = round(adaptation_quality, 1),
            autonomous_balance_score = round(autonomous_balance, 1),
            overreaction_detected    = overreaction_detected,
            underreaction_detected   = underreaction_detected,
            excessive_adaptation     = excessive_adaptation,
            delayed_adaptation       = delayed_adaptation,
            governance_oscillating   = governance_oscillating,
            signals                  = signals,
            cycles_analyzed          = n,
            drift_recommendation     = recommendation,
            evaluated_at             = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_gov_drift.set(governance_drift)
            except Exception:
                pass

        return report

    def _build_recommendation(
        self, overreaction: bool, underreaction: bool, delayed: bool, oscillating: bool
    ) -> str:
        if overreaction and underreaction:
            return "Governanca instavel: overreaction E underreaction simultaneos. Revisar todos os thresholds."
        if overreaction:
            return "Overreaction: aumentar EMERGENCY_DRIFT_THRESH e adicionar confirmacao de 2 ciclos."
        if underreaction:
            return "Underreaction: diminuir EMERGENCY_DRIFT_THRESH ou adicionar sentinel de systemic_risk."
        if delayed:
            return "Delay de adaptacao: acelerar ciclo de governance ou adicionar fast-path para risco alto."
        if oscillating:
            return "Governanca oscilando: implementar hysteresis — exigir N ciclos consecutivos antes de mudar modo."
        return "Qualidade de governanca dentro dos parametros. Sem deriva detectada."

    def _minimal_report(self, n: int) -> GovernanceDriftReport:
        return GovernanceDriftReport(
            governance_drift_score=0.0, adaptation_quality_score=100.0,
            autonomous_balance_score=100.0, overreaction_detected=False,
            underreaction_detected=False, excessive_adaptation=False,
            delayed_adaptation=False, governance_oscillating=False,
            signals=[], cycles_analyzed=n,
            drift_recommendation=f"Apenas {n} ciclos — dados insuficientes para analise de deriva.",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
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

    def _persist(self, report: GovernanceDriftReport) -> None:
        try:
            GOV_DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":           report.evaluated_at,
                "governance_drift_score":  report.governance_drift_score,
                "adaptation_quality_score": report.adaptation_quality_score,
                "autonomous_balance_score": report.autonomous_balance_score,
                "overreaction_detected":   report.overreaction_detected,
                "underreaction_detected":  report.underreaction_detected,
                "delayed_adaptation":      report.delayed_adaptation,
            }
            with open(GOV_DRIFT_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Governance Drift Intelligence — Phase P FASE 9")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    analyzer = GovernanceDriftIntelligence()
    report   = analyzer.analyze()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nGovernance Drift Intelligence")
    print(f"  governance_drift_score:   {report.governance_drift_score:.0f}/100")
    print(f"  adaptation_quality_score: {report.adaptation_quality_score:.0f}/100")
    print(f"  autonomous_balance_score: {report.autonomous_balance_score:.0f}/100")
    print(f"  cycles_analyzed:          {report.cycles_analyzed}")
    print(f"\n  Checks:")
    print(f"    overreaction:          {'DETECTADA' if report.overreaction_detected else 'OK'}")
    print(f"    underreaction:         {'DETECTADA' if report.underreaction_detected else 'OK'}")
    print(f"    excessive_adaptation:  {'DETECTADA' if report.excessive_adaptation else 'OK'}")
    print(f"    delayed_adaptation:    {'DETECTADA' if report.delayed_adaptation else 'OK'}")
    print(f"    governance_oscillating:{'DETECTADA' if report.governance_oscillating else 'OK'}")
    if report.signals:
        print(f"\n  Sinais:")
        for s in report.signals:
            print(f"    [{s.severity.upper()}] {s.signal_type}: {s.description}")
    print(f"\n  -> {report.drift_recommendation}")


if __name__ == "__main__":
    main()

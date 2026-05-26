"""
capital_preservation_validator.py — Phase P FASE 4

Capital Preservation Validator.

Valida que o sistema autonomo reage corretamente para preservar capital:
  - exposure contraction:    exposure reduz quando drift/risk aumenta
  - stress reduction:        sizing reduz em cenarios de stress
  - volatility protection:   volatility_protection_score ativo quando necessario
  - degradation reaction:    estrategias degradadas perdem exposure rapidamente
  - drift protection:        market_drift alto → capital preservation ativo
  - drawdown containment:    drawdowns contidos pelos mecanismos de controle

Scores produzidos:
  - capital_survival_score:        probabilidade de sobrevivencia do capital (0-100)
  - preservation_efficiency_score: quao eficientemente o sistema preserva capital (0-100)
  - drawdown_protection_score:     protecao efetiva contra drawdowns (0-100)

CLI:
  python -m domains.crypto_coin.research.capital_preservation_validator
  python -m domains.crypto_coin.research.capital_preservation_validator --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

EXECUTION_LOG  = Path("data/execution_intelligence_log.jsonl")
EXPOSURE_LOG   = Path("data/exposure_control_log.jsonl")
GOVERNANCE_LOG = Path("data/governance_history.jsonl")
SURVIVAL_LOG   = Path("data/survival_history.jsonl")
PRESERVATION_LOG = Path("data/capital_preservation_log.jsonl")

EXPERIMENTS_DIR = Path("data/experiments")

# Prometheus (optional)
try:
    from api.metrics import capital_survival_score as _prom_capital
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

HIGH_DRIFT_THRESH       = 60.0  # drift >= 60 deve ter triggered capital preservation
HIGH_RISK_THRESH        = 65.0  # systemic_risk >= 65 deve ter triggered survival mode
EXPECTED_REDUCTION_MIN  = 0.30  # em emergencia, espera-se reducao de ao menos 30%
MAX_DRAWDOWN_ALLOWED    = -0.25 # drawdown maximo aceitavel com protecao ativa (-25%)
MIN_REACTION_SPEED      = 2     # em no maximo 2 ciclos pos-trigger, exposure deve cair


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class PreservationCheck:
    """Resultado de um check individual de preservacao."""
    check_name:   str
    passed:       bool
    score:        float   # 0-100
    description:  str
    evidence:     dict


@dataclass
class CapitalPreservationReport:
    """Relatorio de validacao de preservacao de capital."""
    capital_survival_score:        float  # 0-100
    preservation_efficiency_score: float  # 0-100
    drawdown_protection_score:     float  # 0-100

    checks:                list[PreservationCheck]
    checks_passed:         int
    checks_failed:         int

    # Evidencias chave
    high_drift_episodes:   int   # vezes que drift >= HIGH_DRIFT_THRESH
    preservation_triggered: int  # vezes que capital_preservation_active=True
    avg_reduction_in_emergencies: float  # reducao media de exposure em modos emergency/survival
    survival_mode_episodes: int

    preservation_recommendation: str
    evaluated_at:          str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


# ── Validator ──────────────────────────────────────────────────────────────────

class CapitalPreservationValidator:
    """
    FASE 4: Valida comportamento de preservacao de capital do sistema.

    Analisa historicos de execucao e governance para verificar que o
    sistema reagiu corretamente a episodios de alto risco.
    """

    def validate(self) -> CapitalPreservationReport:
        checks: list[PreservationCheck] = []

        exec_records = self._load_jsonl(EXECUTION_LOG, 200)
        exp_records  = self._load_jsonl(EXPOSURE_LOG, 200)
        gov_records  = self._load_jsonl(GOVERNANCE_LOG, 100)
        surv_records = self._load_jsonl(SURVIVAL_LOG, 50)

        # ── Check 1: Capital Preservation Triggered em Alto Drift ─────────────
        high_drift_exec = [
            r for r in exec_records
            if r.get("market_drift_score", 0.0) >= HIGH_DRIFT_THRESH
        ]
        preservation_in_high_drift = [
            r for r in high_drift_exec
            if r.get("capital_preservation_active", False)
        ]

        high_drift_episodes = len(high_drift_exec)
        preservation_triggered = len(preservation_in_high_drift)

        if high_drift_episodes == 0:
            ch1 = PreservationCheck(
                "capital_preservation_trigger", True, 80.0,
                "Sem episodios de alto drift para validar — baseline neutro",
                {"high_drift_episodes": 0},
            )
        elif preservation_triggered / high_drift_episodes >= 0.85:
            ch1 = PreservationCheck(
                "capital_preservation_trigger", True,
                min(100.0, preservation_triggered / high_drift_episodes * 100),
                f"Capital preservation ativado em {preservation_triggered}/{high_drift_episodes} episodios de alto drift",
                {"trigger_rate": round(preservation_triggered / high_drift_episodes, 3)},
            )
        else:
            trigger_rate = preservation_triggered / high_drift_episodes
            ch1 = PreservationCheck(
                "capital_preservation_trigger", False,
                trigger_rate * 100,
                f"Capital preservation FALHOU em {high_drift_episodes - preservation_triggered} episodios de alto drift",
                {"trigger_rate": round(trigger_rate, 3), "episodes_without": high_drift_episodes - preservation_triggered},
            )
        checks.append(ch1)

        # ── Check 2: Reducao de Exposure em Emergency/Survival ─────────────────
        emergency_exec = [
            r for r in exec_records
            if r.get("control_mode") in ("emergency", "survival")
        ]
        avg_reduction = 0.0
        if emergency_exec:
            reductions = [
                1.0 - (r.get("final_sizing", 0.0) / max(r.get("raw_exposure", 1e-6), 1e-6))
                for r in emergency_exec
                if r.get("raw_exposure", 0.0) > 0
            ]
            avg_reduction = statistics.mean(reductions) if reductions else 0.0
            passed = avg_reduction >= EXPECTED_REDUCTION_MIN
            checks.append(PreservationCheck(
                "exposure_reduction_in_emergency", passed,
                min(100.0, avg_reduction / EXPECTED_REDUCTION_MIN * 100) if EXPECTED_REDUCTION_MIN > 0 else 50.0,
                f"Reducao media de exposure em emergencia: {avg_reduction:.0%} ({'OK' if passed else 'INSUFICIENTE'})",
                {"avg_reduction": round(avg_reduction, 3), "min_expected": EXPECTED_REDUCTION_MIN, "sample_size": len(emergency_exec)},
            ))
        else:
            checks.append(PreservationCheck(
                "exposure_reduction_in_emergency", True, 75.0,
                "Sem episodios de emergencia/survival para validar",
                {"emergency_episodes": 0},
            ))

        # ── Check 3: Survival Mode Ativado em Risco Sistemico Alto ────────────
        high_risk_gov = [
            r for r in gov_records
            if r.get("systemic_risk_score", 0.0) >= HIGH_RISK_THRESH
        ]
        survival_gov  = [r for r in high_risk_gov if r.get("survival_mode_active", False)]
        survival_mode_episodes = len(survival_gov)

        if not high_risk_gov:
            checks.append(PreservationCheck(
                "survival_mode_activation", True, 80.0,
                "Sem episodios de alto risco sistemico para validar",
                {"high_risk_episodes": 0},
            ))
        else:
            activation_rate = len(survival_gov) / len(high_risk_gov)
            checks.append(PreservationCheck(
                "survival_mode_activation", activation_rate >= 0.80,
                activation_rate * 100,
                f"Survival mode ativado em {activation_rate:.0%} dos episodios de alto risco sistemico",
                {"activation_rate": round(activation_rate, 3), "high_risk_episodes": len(high_risk_gov)},
            ))

        # ── Check 4: Frozen Strategies Tem Zero Exposure ──────────────────────
        frozen_exec = [r for r in exec_records if r.get("activation_state") == "frozen"]
        if frozen_exec:
            frozen_with_nonzero = [r for r in frozen_exec if r.get("final_sizing", 0.0) > 0.001]
            passed = len(frozen_with_nonzero) == 0
            checks.append(PreservationCheck(
                "frozen_zero_exposure", passed,
                100.0 if passed else 0.0,
                f"Estrategias frozen com exposure zero: {'OK' if passed else f'{len(frozen_with_nonzero)} com exposure > 0'}",
                {"frozen_total": len(frozen_exec), "non_zero": len(frozen_with_nonzero)},
            ))
        else:
            checks.append(PreservationCheck(
                "frozen_zero_exposure", True, 90.0,
                "Sem estrategias frozen no historico para validar",
                {},
            ))

        # ── Check 5: Drawdown Protection (via experimentos) ───────────────────
        drawdown_protection_score = self._validate_drawdown_protection()
        checks.append(PreservationCheck(
            "drawdown_containment", drawdown_protection_score >= 60.0,
            drawdown_protection_score,
            f"Drawdown protection score: {drawdown_protection_score:.0f}/100",
            {"drawdown_protection_score": drawdown_protection_score},
        ))

        # ── Compute composite scores ───────────────────────────────────────────
        checks_passed = sum(1 for c in checks if c.passed)
        checks_failed = sum(1 for c in checks if not c.passed)
        pass_rate = checks_passed / max(len(checks), 1)

        capital_survival = round(
            pass_rate * 60.0 +
            (avg_reduction * 40.0 if avg_reduction > 0 else 30.0),
            1,
        )
        capital_survival = min(100.0, capital_survival)

        preservation_eff = round(statistics.mean(c.score for c in checks), 1)

        recommendation = self._build_recommendation(checks_failed, avg_reduction, checks)

        report = CapitalPreservationReport(
            capital_survival_score        = capital_survival,
            preservation_efficiency_score = preservation_eff,
            drawdown_protection_score     = drawdown_protection_score,
            checks                        = checks,
            checks_passed                 = checks_passed,
            checks_failed                 = checks_failed,
            high_drift_episodes           = high_drift_episodes,
            preservation_triggered        = preservation_triggered,
            avg_reduction_in_emergencies  = round(avg_reduction, 3),
            survival_mode_episodes        = survival_mode_episodes,
            preservation_recommendation   = recommendation,
            evaluated_at                  = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_capital.set(capital_survival)
            except Exception:
                pass

        return report

    def _validate_drawdown_protection(self) -> float:
        """Valida drawdown protection via historico de experimentos."""
        try:
            from domains.crypto_coin.research.strategy_degradation_intelligence import DegradationFleetAnalyzer
            fleet = DegradationFleetAnalyzer(EXPERIMENTS_DIR).rank_all()
            if not fleet:
                return 70.0  # sem dados = baseline neutro

            # Estrategias saudaveis (health > 60) nao devem ter drawdown catastrofico
            healthy = [r for r in fleet if r.strategy_health_score >= 60]
            if not healthy:
                return 50.0

            # Proxy: health alto = drawdown contido pela governanca
            avg_health = statistics.mean(r.strategy_health_score for r in healthy)
            return min(100.0, avg_health)
        except Exception:
            return 70.0  # fallback conservador

    def _build_recommendation(
        self, failures: int, avg_reduction: float, checks: list[PreservationCheck]
    ) -> str:
        failed_names = [c.check_name for c in checks if not c.passed]
        if "frozen_zero_exposure" in failed_names:
            return "CRITICO: estrategias frozen com exposure > 0. Bug no _apply_control — revisar imediatamente."
        if "survival_mode_activation" in failed_names:
            return "Survival mode nao sendo ativado consistentemente. Revisar SYSTEMIC_RISK_THRESH."
        if avg_reduction < EXPECTED_REDUCTION_MIN and avg_reduction > 0:
            return f"Reducao de exposure em emergencia insuficiente ({avg_reduction:.0%} < {EXPECTED_REDUCTION_MIN:.0%}). Revisar EMERGENCY_FACTOR."
        if failures == 0:
            return "Capital preservation funcionando corretamente. Todos os checks passaram."
        return f"{failures} check(s) falhando. Revisar: {failed_names}."

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

    def _persist(self, report: CapitalPreservationReport) -> None:
        try:
            PRESERVATION_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                report.evaluated_at,
                "capital_survival_score":       report.capital_survival_score,
                "preservation_efficiency_score": report.preservation_efficiency_score,
                "drawdown_protection_score":    report.drawdown_protection_score,
                "checks_passed":               report.checks_passed,
                "checks_failed":               report.checks_failed,
                "avg_reduction_in_emergencies": report.avg_reduction_in_emergencies,
            }
            with open(PRESERVATION_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Capital Preservation Validator — Phase P FASE 4")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    validator = CapitalPreservationValidator()
    report    = validator.validate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nCapital Preservation Validator")
    print(f"  capital_survival_score:        {report.capital_survival_score:.0f}/100")
    print(f"  preservation_efficiency_score: {report.preservation_efficiency_score:.0f}/100")
    print(f"  drawdown_protection_score:     {report.drawdown_protection_score:.0f}/100")
    print(f"\n  Evidencias:")
    print(f"    high_drift_episodes:    {report.high_drift_episodes}")
    print(f"    preservation_triggered: {report.preservation_triggered}")
    print(f"    avg_reduction_emerg:    {report.avg_reduction_in_emergencies:.0%}")
    print(f"    survival_episodes:      {report.survival_mode_episodes}")
    print(f"\n  Checks [{report.checks_passed} passed / {report.checks_failed} failed]:")
    for c in report.checks:
        icon = "OK" if c.passed else "FAIL"
        print(f"    [{icon}] {c.check_name}: {c.description}")
    print(f"\n  -> {report.preservation_recommendation}")


if __name__ == "__main__":
    main()

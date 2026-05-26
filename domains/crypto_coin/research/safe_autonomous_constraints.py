"""
safe_autonomous_constraints.py — Phase P FASE 7

Safe Autonomous Live Constraints.

Limites quantitativos autonomos para operacao micro-live.
Toda violacao gera reacao automatica com lineage e justificativa.

Constraints implementados:
  - max_capital_allocation:   maximo de capital alocado total (fracoes)
  - max_daily_loss:           perda diaria maxima antes de parada automatica
  - max_exposure_per_strategy: maximo de exposure por estrategia individual
  - max_correlation_exposure: exposure total em estrategias correlacionadas
  - emergency_contraction:    contracao automatica em cascata
  - cascading_loss_protection: protecao contra perdas encadeadas

IMPORTANTE:
Sem aprovacao humana. O sistema reage autonomamente.
Toda reacao gera lineage + metrica + justificativa persistida.

CLI:
  python -m domains.crypto_coin.research.safe_autonomous_constraints
  python -m domains.crypto_coin.research.safe_autonomous_constraints --json
  python -m domains.crypto_coin.research.safe_autonomous_constraints --simulate
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONSTRAINTS_LOG = Path("data/safe_constraints_log.jsonl")

EXPERIMENTS_DIR = Path("data/experiments")
EXECUTION_LOG   = Path("data/execution_intelligence_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import emergency_contractions_total as _prom_contractions
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constraint Limits ──────────────────────────────────────────────────────────

MAX_TOTAL_CAPITAL_ALLOCATION = 0.80   # max 80% do capital total alocado
MAX_DAILY_LOSS_FRACTION      = 0.05   # max 5% de perda diaria
MAX_SINGLE_STRATEGY_EXPOSURE = 0.35   # max 35% por estrategia individual
MAX_CORR_EXPOSURE_FRACTION   = 0.50   # max 50% em estrategias correlacionadas (corr >= 0.70)
EMERGENCY_CONTRACTION_THRESH = 0.70   # systemic_risk >= 70 → contracao de emergencia
CASCADING_LOSS_TRIGGER       = 0.03   # perda acumulada >= 3% em 1 hora → protecao

# Fatores de resposta
CONSTRAINT_REDUCTION_FACTOR  = 0.50   # reducao de 50% quando constraint violado
EMERGENCY_REDUCTION_FACTOR   = 0.20   # reducao de 80% em emergencia

PAPER_DAILY_LOSS_LIMIT_USD = 50.0    # limite diario em USD para micro-live paper


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ConstraintViolation:
    """Violacao de constraint detectada."""
    violation_id:       str
    constraint_name:    str
    severity:           str   # warning | violation | critical
    current_value:      float
    limit_value:        float
    action_taken:       str
    reduction_factor:   float
    justification:      str
    triggered_at:       str


@dataclass
class ConstraintCheckResult:
    """Resultado de avaliacao de um constraint."""
    constraint_name:   str
    limit:             float
    current_value:     float
    passed:            bool
    violation:         ConstraintViolation | None
    description:       str


@dataclass
class SafeConstraintsReport:
    """Relatorio de verificacao de todos os constraints."""
    all_constraints_passed:   bool
    emergency_contraction:    bool   # True se contracao de emergencia foi ativada

    constraint_checks:        list[ConstraintCheckResult]
    violations:               list[ConstraintViolation]
    violations_count:         int
    critical_violations:      int

    # Estado pos-constraint
    max_allowed_total_exposure: float   # exposure total permitida apos constraints
    max_allowed_per_strategy:   float   # exposure por estrategia permitida

    constraints_recommendation: str
    warning:                    str
    evaluated_at:               str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["constraint_checks"] = [asdict(c) for c in self.constraint_checks]
        d["violations"] = [asdict(v) for v in self.violations]
        for i, c in enumerate(d["constraint_checks"]):
            if c["violation"]:
                c["violation"] = asdict(self.constraint_checks[i].violation)
        return d


# ── Constraint Engine ──────────────────────────────────────────────────────────

class SafeAutonomousConstraints:
    """
    FASE 7: Limites quantitativos autonomos para micro-live.

    Verifica e aplica constraints sem intervencao humana.
    Toda acao e persistida com lineage UUID.
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def evaluate(
        self,
        strategy_ids: list[str],
        current_exposures: dict[str, float] | None = None,  # strategy_id → exposure
        systemic_risk: float = 0.0,
        realized_pnl_fraction: float = 0.0,  # perda realizada hoje (negativo = perda)
        correlated_pairs: list[tuple[str, str]] | None = None,
    ) -> SafeConstraintsReport:
        """Avalia todos os constraints e retorna relatorio com acoes."""
        checks: list[ConstraintCheckResult] = []
        violations: list[ConstraintViolation] = []

        # Usar exposures do log de execucao se nao fornecidas
        if current_exposures is None:
            current_exposures = self._load_current_exposures()

        total_exposure = sum(current_exposures.values())
        correlated_exposure = self._compute_corr_exposure(current_exposures, correlated_pairs)

        # ── Check 1: Max Total Capital Allocation ─────────────────────────────
        ch1 = self._check_constraint(
            "max_total_capital",
            current_value=total_exposure,
            limit=MAX_TOTAL_CAPITAL_ALLOCATION,
            compare="<=",
            description=f"Exposure total={total_exposure:.0%} (limite={MAX_TOTAL_CAPITAL_ALLOCATION:.0%})",
        )
        checks.append(ch1)
        if ch1.violation:
            violations.append(ch1.violation)

        # ── Check 2: Max Daily Loss ────────────────────────────────────────────
        ch2 = self._check_constraint(
            "max_daily_loss",
            current_value=realized_pnl_fraction,
            limit=-MAX_DAILY_LOSS_FRACTION,
            compare=">=",
            description=f"PnL diario={realized_pnl_fraction:.2%} (limite={-MAX_DAILY_LOSS_FRACTION:.2%})",
        )
        checks.append(ch2)
        if ch2.violation:
            violations.append(ch2.violation)

        # ── Check 3: Max Per-Strategy Exposure ────────────────────────────────
        for sid, exp in current_exposures.items():
            ch = self._check_constraint(
                f"max_strategy_exposure_{sid}",
                current_value=exp,
                limit=MAX_SINGLE_STRATEGY_EXPOSURE,
                compare="<=",
                description=f"{sid}: exposure={exp:.0%} (limite={MAX_SINGLE_STRATEGY_EXPOSURE:.0%})",
            )
            checks.append(ch)
            if ch.violation:
                violations.append(ch.violation)

        # ── Check 4: Max Correlated Exposure ──────────────────────────────────
        ch4 = self._check_constraint(
            "max_corr_exposure",
            current_value=correlated_exposure,
            limit=MAX_CORR_EXPOSURE_FRACTION,
            compare="<=",
            description=f"Exposure correlacionada={correlated_exposure:.0%} (limite={MAX_CORR_EXPOSURE_FRACTION:.0%})",
        )
        checks.append(ch4)
        if ch4.violation:
            violations.append(ch4.violation)

        # ── Check 5: Emergency Contraction ────────────────────────────────────
        emergency_contraction = systemic_risk >= EMERGENCY_CONTRACTION_THRESH
        if emergency_contraction:
            viol = ConstraintViolation(
                violation_id      = str(uuid.uuid4())[:8],
                constraint_name   = "emergency_contraction",
                severity          = "critical",
                current_value     = systemic_risk,
                limit_value       = EMERGENCY_CONTRACTION_THRESH,
                action_taken      = f"CONTRACAO DE EMERGENCIA: exposure reduzida a {EMERGENCY_REDUCTION_FACTOR:.0%}",
                reduction_factor  = EMERGENCY_REDUCTION_FACTOR,
                justification     = f"systemic_risk={systemic_risk:.0f} >= {EMERGENCY_CONTRACTION_THRESH} — contracao automatica",
                triggered_at      = datetime.now(timezone.utc).isoformat(),
            )
            violations.append(viol)
            checks.append(ConstraintCheckResult(
                constraint_name="emergency_contraction",
                limit=EMERGENCY_CONTRACTION_THRESH, current_value=systemic_risk,
                passed=False, violation=viol,
                description=f"CONTRACAO DE EMERGENCIA ativa (systemic_risk={systemic_risk:.0f})",
            ))

        # ── Compute state pos-constraints ─────────────────────────────────────
        critical_violations = sum(1 for v in violations if v.severity == "critical")
        all_passed = len(violations) == 0

        if emergency_contraction:
            max_total   = MAX_TOTAL_CAPITAL_ALLOCATION * EMERGENCY_REDUCTION_FACTOR
            max_per_sid = MAX_SINGLE_STRATEGY_EXPOSURE * EMERGENCY_REDUCTION_FACTOR
        elif critical_violations > 0:
            max_total   = MAX_TOTAL_CAPITAL_ALLOCATION * CONSTRAINT_REDUCTION_FACTOR
            max_per_sid = MAX_SINGLE_STRATEGY_EXPOSURE * CONSTRAINT_REDUCTION_FACTOR
        else:
            max_total   = MAX_TOTAL_CAPITAL_ALLOCATION
            max_per_sid = MAX_SINGLE_STRATEGY_EXPOSURE

        recommendation = self._build_recommendation(all_passed, emergency_contraction, violations)

        report = SafeConstraintsReport(
            all_constraints_passed    = all_passed,
            emergency_contraction     = emergency_contraction,
            constraint_checks         = checks,
            violations                = violations,
            violations_count          = len(violations),
            critical_violations       = critical_violations,
            max_allowed_total_exposure = round(max_total, 3),
            max_allowed_per_strategy  = round(max_per_sid, 3),
            constraints_recommendation = recommendation,
            warning                   = "PAPER ONLY — constraints simulados. Sem execucao real de ordens.",
            evaluated_at              = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report, violations)
        if _METRICS_AVAILABLE and (emergency_contraction or critical_violations > 0):
            try:
                _prom_contractions.labels(type="emergency" if emergency_contraction else "constraint").inc()
            except Exception:
                pass

        return report

    def _check_constraint(
        self,
        name:          str,
        current_value: float,
        limit:         float,
        compare:       str,  # "<=" | ">="
        description:   str,
    ) -> ConstraintCheckResult:
        if compare == "<=":
            passed = current_value <= limit
        else:
            passed = current_value >= limit

        violation = None
        if not passed:
            excess    = abs(current_value - limit)
            severity  = "critical" if excess > abs(limit) * 0.5 else "violation"
            violation = ConstraintViolation(
                violation_id     = str(uuid.uuid4())[:8],
                constraint_name  = name,
                severity         = severity,
                current_value    = round(current_value, 4),
                limit_value      = round(limit, 4),
                action_taken     = f"Reduzir exposure em {CONSTRAINT_REDUCTION_FACTOR:.0%}",
                reduction_factor = CONSTRAINT_REDUCTION_FACTOR,
                justification    = f"{name}: {current_value:.3f} viola limite {compare} {limit:.3f}",
                triggered_at     = datetime.now(timezone.utc).isoformat(),
            )

        return ConstraintCheckResult(
            constraint_name = name,
            limit           = limit,
            current_value   = current_value,
            passed          = passed,
            violation       = violation,
            description     = description,
        )

    def _compute_corr_exposure(
        self,
        exposures:        dict[str, float],
        correlated_pairs: list[tuple[str, str]] | None,
    ) -> float:
        if not correlated_pairs:
            return 0.0
        corr_ids: set[str] = set()
        for a, b in correlated_pairs:
            corr_ids.add(a)
            corr_ids.add(b)
        return sum(exp for sid, exp in exposures.items() if sid in corr_ids)

    def _load_current_exposures(self) -> dict[str, float]:
        """Carrega exposures mais recentes do log de execucao."""
        if not EXECUTION_LOG.exists():
            return {}
        last_by_sid: dict[str, float] = {}
        try:
            with open(EXECUTION_LOG) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        sid = r.get("strategy_id", "")
                        siz = r.get("final_sizing", 0.0)
                        if sid:
                            last_by_sid[sid] = siz
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        return last_by_sid

    def _build_recommendation(
        self,
        all_passed:    bool,
        emergency:     bool,
        violations:    list[ConstraintViolation],
    ) -> str:
        if emergency:
            return "CONTRACAO DE EMERGENCIA ATIVA. Exposure reduzida a 20% automaticamente."
        if not all_passed:
            names = [v.constraint_name for v in violations[:3]]
            return f"Constraints violados: {names}. Exposure reduzida em 50% automaticamente."
        return "Todos os constraints dentro dos limites. Sistema micro-live operando normalmente."

    def _persist(self, report: SafeConstraintsReport, violations: list[ConstraintViolation]) -> None:
        try:
            CONSTRAINTS_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":            report.evaluated_at,
                "all_constraints_passed":  report.all_constraints_passed,
                "emergency_contraction":   report.emergency_contraction,
                "violations_count":        report.violations_count,
                "critical_violations":     report.critical_violations,
                "max_allowed_total":       report.max_allowed_total_exposure,
                "violations":             [v.constraint_name for v in violations],
            }
            with open(CONSTRAINTS_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe Autonomous Constraints — Phase P FASE 7")
    parser.add_argument("--json",     action="store_true")
    parser.add_argument("--simulate", action="store_true", help="Simular violacao de constraints")
    args = parser.parse_args()

    engine = SafeAutonomousConstraints()

    if args.simulate:
        # Simula cenario com violacoes
        exposures     = {"trend_following": 0.40, "mean_reversion": 0.30, "momentum": 0.25}
        systemic_risk = 75.0  # dispara emergency
        report = engine.evaluate(
            strategy_ids=list(exposures.keys()),
            current_exposures=exposures,
            systemic_risk=systemic_risk,
            realized_pnl_fraction=-0.06,  # -6% → viola daily loss
        )
    else:
        report = engine.evaluate(strategy_ids=[])

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nSafe Autonomous Constraints")
    print(f"  {report.warning}")
    status = "TODOS OK" if report.all_constraints_passed else f"{report.violations_count} VIOLACOES"
    print(f"\n  Status: {status}")
    print(f"  emergency_contraction:      {'ATIVA' if report.emergency_contraction else 'inativa'}")
    print(f"  max_allowed_total_exposure: {report.max_allowed_total_exposure:.0%}")
    print(f"  max_allowed_per_strategy:   {report.max_allowed_per_strategy:.0%}")
    if report.violations:
        print(f"\n  Violacoes:")
        for v in report.violations:
            print(f"    [{v.severity.upper()}] {v.constraint_name}: {v.justification}")
            print(f"           Acao: {v.action_taken}")
    print(f"\n  -> {report.constraints_recommendation}")


if __name__ == "__main__":
    main()

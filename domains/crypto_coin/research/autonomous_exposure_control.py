"""
autonomous_exposure_control.py — Phase O FASE 3

Autonomous Exposure Control.

Controle autônomo de exposição com auto-throttling e capital preservation.
Estende AdaptiveExposureIntelligence (Phase N FASE 4) com:
  - emergency_exposure_score:     nível de emergência de exposure (0=ok, 100=emergency)
  - survival_mode_score:          ativação de modo de sobrevivência (0–100)
  - volatility_protection_score:  proteção contra volatilidade extrema (0–100)

Self-throttling automático:
  - Reduz automaticamente exposure quando emergência detectada
  - Persiste cada decisão com lineage e justificativa quantitativa
  - PAPER ONLY — gera controle, não executa trades reais

CLI:
  python -m domains.crypto_coin.research.autonomous_exposure_control --all
  python -m domains.crypto_coin.research.autonomous_exposure_control --strategies trend_following
  python -m domains.crypto_coin.research.autonomous_exposure_control --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.adaptive_exposure_intelligence import AdaptiveExposureIntelligence
from domains.crypto_coin.research.market_drift_intelligence import MarketDriftIntelligence
from domains.crypto_coin.research.strategy_degradation_intelligence import DegradationFleetAnalyzer
from domains.crypto_coin.research.strategy_activation_engine import StrategyActivationEngine

EXPERIMENTS_DIR       = Path("data/experiments")
EXPOSURE_CONTROL_LOG  = Path("data/exposure_control_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import (
        market_survival_score as _prom_survival,
        adaptive_exposure_score as _prom_exposure,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────

# Emergency thresholds
EMERGENCY_DRIFT_THRESH     = 65.0   # drift >= 65 → emergency
EMERGENCY_RISK_THRESH      = 70.0   # fleet avg risk >= 70 → emergency
SURVIVAL_DRIFT_THRESH      = 80.0   # drift >= 80 → survival mode
SURVIVAL_HEALTH_THRESH     = 30.0   # fleet health <= 30 → survival mode

# Exposure reduction factors
THROTTLE_FACTOR      = 0.60   # exposure × 0.60 em throttle
EMERGENCY_FACTOR     = 0.35   # exposure × 0.35 em emergency
SURVIVAL_FACTOR      = 0.15   # exposure × 0.15 em survival mode
VOLATILITY_FACTOR    = 0.50   # exposure × 0.50 em high volatility


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ExposureControlDecision:
    """Decisão de controle de exposição para uma estratégia."""
    decision_id:              str
    strategy_id:              str

    # Scores de emergência
    emergency_exposure_score:   float   # 0–100 (100 = emergência máxima)
    survival_mode_score:        float   # 0–100 (100 = survival mode ativo)
    volatility_protection_score: float  # 0–100 (100 = máxima proteção)

    # Exposure resultante
    requested_exposure:    float   # 0.0–1.0 antes do controle
    controlled_exposure:   float   # 0.0–1.0 após aplicação de controles
    reduction_factor:      float   # quanto foi reduzido (1.0 = sem redução)
    control_mode:          str     # normal | throttled | emergency | survival

    # Drivers
    market_drift_score:    float
    fleet_health_avg:      float
    activation_state:      str

    justification:         str
    occurred_at:           str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExposureControlReport:
    """Relatório de controle de exposição de toda a frota."""
    decisions:              list[ExposureControlDecision]
    fleet_control_mode:     str   # normal | throttled | emergency | survival
    market_drift_score:     float
    fleet_health_avg:       float
    total_controlled_exposure: float  # soma de controlled_exposure
    capital_preservation_factor: float  # quanto capital foi preservado vs. pedido
    recommendation:         str
    computed_at:            str
    warning:                str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decisions"] = [asdict(dc) for dc in self.decisions]
        return d


# ── Controller ────────────────────────────────────────────────────────────────

class AutonomousExposureControl:
    """
    FASE 3: Controle autônomo de exposição com self-throttling.

    Hierarquia de modos (do mais restritivo para o menos):
      survival  → exposure × 0.15 (drift ≥ 80 OU fleet health ≤ 30)
      emergency → exposure × 0.35 (drift ≥ 65 OU fleet risk ≥ 70)
      throttled → exposure × 0.60 (sinais moderados)
      normal    → exposure sem redução adicional além dos caps de lifecycle

    Cada decisão persiste em data/exposure_control_log.jsonl.
    """

    def __init__(
        self,
        experiments_dir:  Path = EXPERIMENTS_DIR,
        control_log:      Path = EXPOSURE_CONTROL_LOG,
        current_regime:   str | None = None,
    ):
        self.experiments_dir = experiments_dir
        self.control_log     = control_log
        self.current_regime  = current_regime

    def control(self, strategy_ids: list[str]) -> ExposureControlReport:
        """Executa controle de exposição para a frota."""

        # Drift e saúde da frota
        drift_report   = MarketDriftIntelligence(self.experiments_dir).analyze()
        market_drift   = drift_report.market_drift_score
        fleet_health   = drift_report.fleet_health_avg

        # Base exposure da Phase N (sem controle de emergência)
        base_exposure_engine = AdaptiveExposureIntelligence(
            experiments_dir=self.experiments_dir,
            current_regime=self.current_regime,
        )
        base_report = base_exposure_engine.analyze(strategy_ids)

        # Fleet risk (average composite_risk)
        fleet_analyzer = DegradationFleetAnalyzer(self.experiments_dir)
        fleet_reports  = fleet_analyzer.rank_all()
        fleet_risks    = [r.composite_risk_score for r in fleet_reports if r.strategy_id in strategy_ids]
        fleet_avg_risk = statistics.mean(fleet_risks) if fleet_risks else 0.0

        # Activation states
        activation_engine = StrategyActivationEngine(self.experiments_dir)

        # ── Determinar modo de controle global ────────────────────────────────
        fleet_control_mode = self._determine_control_mode(market_drift, fleet_health, fleet_avg_risk)

        decisions: list[ExposureControlDecision] = []

        for base_rec in base_report.strategies:
            sid = base_rec.strategy_id

            # Activation state
            try:
                act_status = activation_engine.evaluate(sid)
                act_state  = act_status.activation_state
            except Exception:
                act_state = "active"

            # Scores de emergência para esta estratégia
            emergency_score   = self._compute_emergency_score(market_drift, base_rec.composite_risk)
            survival_score    = self._compute_survival_score(market_drift, fleet_health)
            volatility_score  = self._compute_volatility_protection_score(base_rec.fragility_score, market_drift)

            # Exposure base desta estratégia
            base_exp = base_rec.max_exposure_fraction

            # Aplicar controle
            controlled_exp, reduction, control_mode = self._apply_control(
                base_exposure  = base_exp,
                fleet_mode     = fleet_control_mode,
                act_state      = act_state,
                emergency_score= emergency_score,
            )

            justification = self._build_justification(
                fleet_mode, control_mode, market_drift, fleet_health, act_state
            )

            decision = ExposureControlDecision(
                decision_id                = str(uuid.uuid4())[:8],
                strategy_id               = sid,
                emergency_exposure_score  = round(emergency_score, 1),
                survival_mode_score       = round(survival_score, 1),
                volatility_protection_score = round(volatility_score, 1),
                requested_exposure        = round(base_exp, 3),
                controlled_exposure       = round(controlled_exp, 3),
                reduction_factor          = round(reduction, 3),
                control_mode              = control_mode,
                market_drift_score        = round(market_drift, 1),
                fleet_health_avg          = round(fleet_health, 1),
                activation_state          = act_state,
                justification             = justification,
                occurred_at               = datetime.now(timezone.utc).isoformat(),
            )
            decisions.append(decision)
            self._persist_decision(decision)

        total_controlled = sum(d.controlled_exposure for d in decisions)
        total_requested  = sum(d.requested_exposure for d in decisions)
        preservation     = (1.0 - total_controlled / total_requested) if total_requested > 0 else 0.0

        recommendation = self._fleet_recommendation(fleet_control_mode, market_drift, fleet_health)

        # Emite métricas
        if _METRICS_AVAILABLE:
            try:
                avg_exposure = statistics.mean(d.controlled_exposure for d in decisions) * 100 if decisions else 0.0
                _prom_exposure.set(avg_exposure)
            except Exception:
                pass

        return ExposureControlReport(
            decisions                  = decisions,
            fleet_control_mode         = fleet_control_mode,
            market_drift_score         = round(market_drift, 1),
            fleet_health_avg           = round(fleet_health, 1),
            total_controlled_exposure  = round(total_controlled, 3),
            capital_preservation_factor= round(preservation, 3),
            recommendation             = recommendation,
            computed_at                = datetime.now(timezone.utc).isoformat(),
            warning                    = "⚠️ PAPER ONLY — Controle de exposure autônomo. Sem execução real.",
        )

    # ── Mode and score computations ───────────────────────────────────────────

    def _determine_control_mode(
        self, drift: float, health: float, avg_risk: float
    ) -> str:
        if drift >= SURVIVAL_DRIFT_THRESH or health <= SURVIVAL_HEALTH_THRESH:
            return "survival"
        if drift >= EMERGENCY_DRIFT_THRESH or avg_risk >= EMERGENCY_RISK_THRESH:
            return "emergency"
        if drift >= 40 or avg_risk >= 50:
            return "throttled"
        return "normal"

    def _apply_control(
        self,
        base_exposure:  float,
        fleet_mode:     str,
        act_state:      str,
        emergency_score: float,
    ) -> tuple[float, float, str]:
        """Aplica controle de exposure. Retorna (controlled, reduction_factor, mode)."""

        # Estratégias frozen/retired → zero exposure
        if act_state in ("frozen", "retired"):
            return 0.0, 0.0, "frozen"

        # Determina modo mais restritivo entre fleet e individual
        mode = fleet_mode
        if emergency_score >= 80 and mode not in ("emergency", "survival"):
            mode = "emergency"

        factors = {
            "survival":  SURVIVAL_FACTOR,
            "emergency": EMERGENCY_FACTOR,
            "throttled": THROTTLE_FACTOR,
            "frozen":    0.0,
            "normal":    1.0,
        }
        factor = factors.get(mode, 1.0)

        # Throttled strategies get additional reduction
        if act_state == "throttled":
            factor = min(factor, THROTTLE_FACTOR)

        controlled = max(0.0, min(1.0, base_exposure * factor))
        return controlled, factor, mode

    def _compute_emergency_score(self, drift: float, strategy_risk: float) -> float:
        return min(100.0, drift * 0.5 + strategy_risk * 0.5)

    def _compute_survival_score(self, drift: float, health: float) -> float:
        drift_component  = max(0.0, drift - 50.0) * 2.0   # 50→100 maps 0→100
        health_component = max(0.0, 50.0 - health) * 2.0  # 50→0 maps 0→100
        return min(100.0, drift_component * 0.5 + health_component * 0.5)

    def _compute_volatility_protection_score(self, fragility: float, drift: float) -> float:
        return min(100.0, fragility * 0.6 + drift * 0.4)

    def _build_justification(
        self, fleet_mode: str, control_mode: str, drift: float, health: float, act_state: str
    ) -> str:
        parts = [f"fleet_mode={fleet_mode}"]
        if control_mode == "survival":
            parts.append(f"SURVIVAL: drift={drift:.0f} ou health={health:.0f}")
        elif control_mode == "emergency":
            parts.append(f"EMERGENCY: drift={drift:.0f}")
        elif control_mode == "throttled":
            parts.append(f"throttled: sinais de risco moderado")
        if act_state in ("frozen", "retired"):
            parts.append(f"activation_state={act_state}")
        return "; ".join(parts)

    def _fleet_recommendation(self, mode: str, drift: float, health: float) -> str:
        if mode == "survival":
            return (
                f"MODO DE SOBREVIVÊNCIA ATIVO (drift={drift:.0f}, health={health:.0f}). "
                "Exposure reduzida a 15% do normal. Revisar todas as estratégias imediatamente."
            )
        if mode == "emergency":
            return (
                f"MODO DE EMERGÊNCIA (drift={drift:.0f}). "
                "Exposure reduzida a 35%. Investigar causa antes do próximo ciclo."
            )
        if mode == "throttled":
            return f"Modo throttled ativo. Exposure reduzida a 60% como precaução."
        return "Condições normais. Controle de exposure padrão ativo."

    def _persist_decision(self, decision: ExposureControlDecision) -> None:
        try:
            self.control_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.control_log, "a") as f:
                f.write(json.dumps(decision.to_dict()) + "\n")
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Exposure Control — Phase O FASE 3"
    )
    parser.add_argument("--strategies", nargs="+", help="strategy_ids")
    parser.add_argument("--all",    action="store_true")
    parser.add_argument("--regime", help="Regime atual")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    strategy_ids = args.strategies or (
        [f.stem for f in EXPERIMENTS_DIR.glob("*.jsonl") if f.stem != "all_experiments"]
        if args.all else []
    )
    if not strategy_ids:
        parser.print_help()
        return

    controller = AutonomousExposureControl(current_regime=args.regime)
    report = controller.control(strategy_ids)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\n⚠️  {report.warning}")
    print(f"\nAutonomous Exposure Control — {len(report.decisions)} estratégias")
    print(f"  fleet_mode:           {report.fleet_control_mode}")
    print(f"  market_drift:         {report.market_drift_score:.0f}/100")
    print(f"  fleet_health:         {report.fleet_health_avg:.0f}/100")
    print(f"  total_controlled_exp: {report.total_controlled_exposure:.1%}")
    print(f"  capital_preserved:    {report.capital_preservation_factor:.1%} redução vs. pedido")
    print(f"\n{'Estratégia':<25} {'Modo':<12} {'Req':>6} {'Ctrl':>6} {'Emergency':>10}")
    print("-" * 65)
    for d in report.decisions:
        print(
            f"{d.strategy_id:<25} {d.control_mode:<12} "
            f"{d.requested_exposure:>6.1%} {d.controlled_exposure:>6.1%} "
            f"{d.emergency_exposure_score:>10.0f}"
        )
    print(f"\n  → {report.recommendation}")


if __name__ == "__main__":
    main()

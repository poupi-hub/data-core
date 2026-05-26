"""
autonomous_execution_intelligence.py — Phase O FASE 8

Autonomous Execution Intelligence.

Consolida todas as decisoes de execucao em um unico ponto com:
  - sizing:             quanto alocar por estrategia (fraction of capital)
  - exposure:           nivel de exposicao controlada (pos emergencia/survival)
  - activation:         estado de ativacao autonomo (active/throttled/frozen/retired)
  - allocation:         distribuicao otima entre estrategias ativas
  - throttling:         reducao automatica em condicoes adversas
  - capital_preservation: preservacao ativa de capital em modo critico

Scores produzidos:
  - execution_confidence_score: confianca na decisao de execucao (0-100)
  - sizing_quality_score:       qualidade do sizing calculado (0-100)
  - capital_efficiency_score:   eficiencia de uso do capital (0-100)

Toda execucao gera:
  - lineage UUID por decisao
  - justificativa quantitativa completa
  - reasoning persistido em data/execution_intelligence_log.jsonl
  - observabilidade via Prometheus

CLI:
  python -m domains.crypto_coin.research.autonomous_execution_intelligence --all
  python -m domains.crypto_coin.research.autonomous_execution_intelligence --strategies s1 s2
  python -m domains.crypto_coin.research.autonomous_execution_intelligence --json
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

from domains.crypto_coin.research.autonomous_exposure_control import AutonomousExposureControl
from domains.crypto_coin.research.strategy_activation_engine import StrategyActivationEngine
from domains.crypto_coin.research.market_drift_intelligence import MarketDriftIntelligence
from domains.crypto_coin.research.strategy_degradation_intelligence import DegradationFleetAnalyzer
from domains.crypto_coin.research.meta_strategy_intelligence import MetaStrategyIntelligence

EXPERIMENTS_DIR   = Path("data/experiments")
EXECUTION_LOG     = Path("data/execution_intelligence_log.jsonl")

# Prometheus (optional)
try:
    from api.metrics import (
        adaptive_exposure_score as _prom_exposure,
        autonomous_execution_total as _prom_exec_total,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

# Sizing caps by activation state
SIZING_CAP_ACTIVE    = 1.00
SIZING_CAP_THROTTLED = 0.50
SIZING_CAP_FROZEN    = 0.00
SIZING_CAP_RETIRED   = 0.00

# Allocation concentration limits
MAX_SINGLE_STRATEGY_ALLOC = 0.40   # max 40% do capital em uma estrategia
MIN_ACTIVE_STRATEGIES     = 2      # minimo de estrategias para alocar

# Capital preservation triggers
CAPITAL_PRESERVE_DRIFT    = 70.0   # drift >= 70 -> capital preservation ativo
CAPITAL_PRESERVE_HEALTH   = 35.0   # fleet health <= 35 -> capital preservation ativo


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ExecutionDecision:
    """Decisao de execucao atomica para uma estrategia."""
    decision_id:              str
    strategy_id:              str

    # Scores de execucao
    execution_confidence_score: float   # 0-100
    sizing_quality_score:       float   # 0-100
    capital_efficiency_score:   float   # 0-100

    # Decisoes quantitativas
    activation_state:     str     # active | throttled | frozen | retired
    control_mode:         str     # normal | throttled | emergency | survival
    raw_exposure:         float   # 0.0-1.0 sem controles de emergencia
    controlled_exposure:  float   # 0.0-1.0 apos controles
    final_sizing:         float   # 0.0-1.0 fração do capital alocada
    allocation_weight:    float   # peso relativo na frota (normalizado)

    # Inputs do modelo
    market_drift_score:   float
    fleet_health_avg:     float
    degradation_score:    float
    strategy_health_score: float

    # Capital preservation
    capital_preservation_active: bool
    throttle_reason:      str | None

    # Lineage e auditoria
    justification:        str
    reasoning:            dict   # breakdown quantitativo
    decided_at:           str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionIntelligenceReport:
    """Relatorio consolidado de execucao da frota."""
    execution_confidence_score: float   # media ponderada
    sizing_quality_score:       float
    capital_efficiency_score:   float

    decisions:                  list[ExecutionDecision]
    fleet_control_mode:         str
    market_drift_score:         float
    fleet_health_avg:           float

    strategies_active:          int
    strategies_throttled:       int
    strategies_frozen:          int
    strategies_retired:         int

    total_allocated_capital:    float   # soma de final_sizing
    capital_preservation_active: bool
    capital_saved_fraction:     float   # quanto capital foi preservado vs. raw

    dominant_risk:              str | None
    recommendation:             str
    warning:                    str
    computed_at:                str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decisions"] = [asdict(dc) for dc in self.decisions]
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class AutonomousExecutionIntelligence:
    """
    FASE 8: Consolida todas as decisoes de execucao com lineage e justificativa.

    Pipeline:
      1. Coleta estado de ativacao (StrategyActivationEngine)
      2. Coleta exposure controlada (AutonomousExposureControl)
      3. Coleta degradacao e saude (DegradationFleetAnalyzer)
      4. Calcula sizing por estrategia (com caps por estado)
      5. Normaliza allocation weights (com cap de concentracao)
      6. Aplica capital preservation se necessario
      7. Computa scores de qualidade e confianca
      8. Persiste com lineage completo
    """

    def __init__(
        self,
        experiments_dir: Path = EXPERIMENTS_DIR,
        execution_log:   Path = EXECUTION_LOG,
        current_regime:  str | None = None,
    ):
        self.experiments_dir = experiments_dir
        self.execution_log   = execution_log
        self.current_regime  = current_regime

    def execute(self, strategy_ids: list[str]) -> ExecutionIntelligenceReport:
        """Gera decisoes de execucao para a frota."""
        if not strategy_ids:
            return self._empty_report()

        # ── Coleta de dados ────────────────────────────────────────────────────
        drift_report  = MarketDriftIntelligence(self.experiments_dir).analyze()
        market_drift  = drift_report.market_drift_score
        fleet_health  = drift_report.fleet_health_avg

        exposure_ctrl = AutonomousExposureControl(
            experiments_dir=self.experiments_dir,
            current_regime=self.current_regime,
        )
        exposure_report = exposure_ctrl.control(strategy_ids)

        activation_engine = StrategyActivationEngine(self.experiments_dir)
        fleet_degradation = DegradationFleetAnalyzer(self.experiments_dir).rank_all()
        degrad_map = {r.strategy_id: r for r in fleet_degradation}

        # Capital preservation check
        capital_preservation_active = (
            market_drift >= CAPITAL_PRESERVE_DRIFT or
            fleet_health <= CAPITAL_PRESERVE_HEALTH
        )

        fleet_control_mode = exposure_report.fleet_control_mode
        decisions: list[ExecutionDecision] = []

        # ── Decisao por estrategia ─────────────────────────────────────────────
        exposure_map = {d.strategy_id: d for d in exposure_report.decisions}

        for sid in strategy_ids:
            exp_dec = exposure_map.get(sid)
            degrad  = degrad_map.get(sid)

            controlled_exposure = exp_dec.controlled_exposure if exp_dec else 0.0
            control_mode        = exp_dec.control_mode if exp_dec else "normal"
            raw_exposure        = exp_dec.requested_exposure if exp_dec else 0.0

            degradation_score   = degrad.degradation_score if degrad else 50.0
            health_score        = degrad.strategy_health_score if degrad else 50.0

            # Activation state
            try:
                act_status   = activation_engine.evaluate(sid)
                act_state    = act_status.activation_state
            except Exception:
                act_state = "active"

            # Sizing com cap por estado
            sizing_cap = {
                "active":    SIZING_CAP_ACTIVE,
                "throttled": SIZING_CAP_THROTTLED,
                "frozen":    SIZING_CAP_FROZEN,
                "retired":   SIZING_CAP_RETIRED,
            }.get(act_state, SIZING_CAP_ACTIVE)

            final_sizing = min(controlled_exposure, sizing_cap)

            # Capital preservation: reduz sizing adicional em modo critico
            if capital_preservation_active and act_state not in ("frozen", "retired"):
                preservation_factor = max(0.20, 1.0 - (market_drift - 50.0) / 100.0)
                final_sizing = final_sizing * preservation_factor

            final_sizing = max(0.0, min(1.0, final_sizing))

            # Scores de qualidade
            exec_confidence = self._compute_execution_confidence(
                act_state, control_mode, degradation_score, market_drift
            )
            sizing_quality = self._compute_sizing_quality(
                final_sizing, raw_exposure, act_state, degradation_score
            )
            capital_eff = self._compute_capital_efficiency(
                final_sizing, health_score, degradation_score
            )

            # Throttle reason
            throttle_reason = None
            if act_state in ("frozen", "retired"):
                throttle_reason = f"activation_state={act_state}"
            elif control_mode in ("emergency", "survival"):
                throttle_reason = f"control_mode={control_mode} (drift={market_drift:.0f})"
            elif capital_preservation_active:
                throttle_reason = f"capital_preservation (drift={market_drift:.0f}, health={fleet_health:.0f})"

            justification = self._build_justification(
                act_state, control_mode, market_drift, fleet_health,
                degradation_score, capital_preservation_active,
            )

            reasoning = {
                "raw_exposure":         round(raw_exposure, 3),
                "controlled_exposure":  round(controlled_exposure, 3),
                "sizing_cap":           sizing_cap,
                "capital_preservation": capital_preservation_active,
                "final_sizing":         round(final_sizing, 3),
                "market_drift":         round(market_drift, 1),
                "fleet_health":         round(fleet_health, 1),
                "degradation_score":    round(degradation_score, 1),
                "strategy_health":      round(health_score, 1),
            }

            decision = ExecutionDecision(
                decision_id                 = str(uuid.uuid4())[:8],
                strategy_id                 = sid,
                execution_confidence_score  = round(exec_confidence, 1),
                sizing_quality_score        = round(sizing_quality, 1),
                capital_efficiency_score    = round(capital_eff, 1),
                activation_state            = act_state,
                control_mode                = control_mode,
                raw_exposure                = round(raw_exposure, 3),
                controlled_exposure         = round(controlled_exposure, 3),
                final_sizing                = round(final_sizing, 3),
                allocation_weight           = 0.0,  # normalizado apos
                market_drift_score          = round(market_drift, 1),
                fleet_health_avg            = round(fleet_health, 1),
                degradation_score           = round(degradation_score, 1),
                strategy_health_score       = round(health_score, 1),
                capital_preservation_active = capital_preservation_active,
                throttle_reason             = throttle_reason,
                justification               = justification,
                reasoning                   = reasoning,
                decided_at                  = datetime.now(timezone.utc).isoformat(),
            )
            decisions.append(decision)

        # ── Normalizar allocation weights ──────────────────────────────────────
        decisions = self._normalize_weights(decisions)

        # ── Scores de frota ────────────────────────────────────────────────────
        active_decisions = [d for d in decisions if d.final_sizing > 0]
        avg_confidence   = statistics.mean(d.execution_confidence_score for d in decisions) if decisions else 0.0
        avg_sizing_qual  = statistics.mean(d.sizing_quality_score for d in decisions) if decisions else 0.0
        avg_capital_eff  = statistics.mean(d.capital_efficiency_score for d in decisions) if decisions else 0.0

        strategies_active    = sum(1 for d in decisions if d.activation_state == "active")
        strategies_throttled = sum(1 for d in decisions if d.activation_state == "throttled")
        strategies_frozen    = sum(1 for d in decisions if d.activation_state == "frozen")
        strategies_retired   = sum(1 for d in decisions if d.activation_state == "retired")

        total_allocated = sum(d.final_sizing for d in decisions)
        total_raw       = sum(d.raw_exposure for d in decisions)
        capital_saved   = max(0.0, 1.0 - total_allocated / total_raw) if total_raw > 0 else 0.0

        dominant_risk = self._find_dominant_risk(decisions, market_drift, fleet_health)
        recommendation = self._build_recommendation(fleet_control_mode, capital_preservation_active, strategies_frozen)

        report = ExecutionIntelligenceReport(
            execution_confidence_score  = round(avg_confidence, 1),
            sizing_quality_score        = round(avg_sizing_qual, 1),
            capital_efficiency_score    = round(avg_capital_eff, 1),
            decisions                   = decisions,
            fleet_control_mode          = fleet_control_mode,
            market_drift_score          = round(market_drift, 1),
            fleet_health_avg            = round(fleet_health, 1),
            strategies_active           = strategies_active,
            strategies_throttled        = strategies_throttled,
            strategies_frozen           = strategies_frozen,
            strategies_retired          = strategies_retired,
            total_allocated_capital     = round(total_allocated, 3),
            capital_preservation_active = capital_preservation_active,
            capital_saved_fraction      = round(capital_saved, 3),
            dominant_risk               = dominant_risk,
            recommendation              = recommendation,
            warning                     = "PAPER ONLY — Execucao autonoma simulada. Sem alocacao real.",
            computed_at                 = datetime.now(timezone.utc).isoformat(),
        )

        # Persist all decisions
        for d in decisions:
            self._persist_decision(d)

        # Prometheus
        if _METRICS_AVAILABLE:
            try:
                avg_exp = statistics.mean(d.controlled_exposure for d in decisions) * 100 if decisions else 0.0
                _prom_exposure.set(avg_exp)
                _prom_exec_total.labels(type="execution_cycle").inc()
            except Exception:
                pass

        return report

    # ── Score computations ─────────────────────────────────────────────────────

    def _compute_execution_confidence(
        self,
        act_state:      str,
        control_mode:   str,
        degradation:    float,
        drift:          float,
    ) -> float:
        base = 100.0
        # Penalidades por estado critico
        if act_state == "frozen":     base -= 60.0
        elif act_state == "throttled": base -= 25.0
        elif act_state == "retired":  base -= 80.0
        if control_mode == "survival":  base -= 30.0
        elif control_mode == "emergency": base -= 20.0
        elif control_mode == "throttled": base -= 10.0
        # Penalidade por degradacao
        base -= max(0.0, (degradation - 50.0) * 0.4)
        # Penalidade por drift
        base -= max(0.0, (drift - 50.0) * 0.3)
        return max(0.0, min(100.0, base))

    def _compute_sizing_quality(
        self,
        final_sizing:   float,
        raw_exposure:   float,
        act_state:      str,
        degradation:    float,
    ) -> float:
        """Qualidade do sizing: penaliza over-reduction e over-allocation."""
        if act_state in ("frozen", "retired"):
            return 100.0 if final_sizing == 0.0 else 0.0
        if raw_exposure == 0.0:
            return 50.0
        ratio = final_sizing / raw_exposure
        # Ideal: ratio entre 0.4 e 1.0 dependendo de degradacao
        ideal = max(0.4, 1.0 - degradation / 150.0)
        deviation = abs(ratio - ideal)
        quality = max(0.0, 100.0 - deviation * 100.0)
        return min(100.0, quality)

    def _compute_capital_efficiency(
        self,
        final_sizing:    float,
        health_score:    float,
        degradation:     float,
    ) -> float:
        """Eficiencia: alto sizing em estrategias saudaveis = eficiente."""
        if final_sizing == 0.0:
            return 100.0 if degradation >= 60 else 50.0
        # Pondera sizing pela saude da estrategia
        efficiency = final_sizing * (health_score / 100.0) * 100.0
        return min(100.0, max(0.0, efficiency))

    def _normalize_weights(self, decisions: list[ExecutionDecision]) -> list[ExecutionDecision]:
        """Normaliza allocation_weight com cap de concentracao."""
        total = sum(d.final_sizing for d in decisions)
        if total == 0.0:
            for d in decisions:
                d.allocation_weight = 0.0
            return decisions

        # Primeira passagem: pesos brutos
        for d in decisions:
            d.allocation_weight = d.final_sizing / total

        # Aplicar cap de concentracao (MAX_SINGLE_STRATEGY_ALLOC)
        capped = False
        for d in decisions:
            if d.allocation_weight > MAX_SINGLE_STRATEGY_ALLOC:
                d.allocation_weight = MAX_SINGLE_STRATEGY_ALLOC
                capped = True

        # Renormalizar se houve cap
        if capped:
            new_total = sum(d.allocation_weight for d in decisions)
            if new_total > 0:
                for d in decisions:
                    d.allocation_weight = round(d.allocation_weight / new_total, 4)

        # Round
        for d in decisions:
            d.allocation_weight = round(d.allocation_weight, 4)

        return decisions

    def _find_dominant_risk(
        self,
        decisions:    list[ExecutionDecision],
        market_drift: float,
        fleet_health: float,
    ) -> str | None:
        if fleet_health <= CAPITAL_PRESERVE_HEALTH:
            return "fleet_health_critical"
        if market_drift >= 80:
            return "market_drift_extreme"
        frozen_count = sum(1 for d in decisions if d.activation_state == "frozen")
        if frozen_count >= len(decisions) * 0.5:
            return "mass_freezing"
        avg_degrad = statistics.mean(d.degradation_score for d in decisions) if decisions else 0.0
        if avg_degrad >= 65:
            return "fleet_degradation"
        return None

    def _build_justification(
        self,
        act_state:    str,
        control_mode: str,
        drift:        float,
        health:       float,
        degradation:  float,
        capital_pres: bool,
    ) -> str:
        parts = [f"control={control_mode}", f"activation={act_state}"]
        if capital_pres:
            parts.append(f"capital_preservation_ON(drift={drift:.0f},health={health:.0f})")
        if degradation >= 60:
            parts.append(f"degradation={degradation:.0f}")
        return "; ".join(parts)

    def _build_recommendation(
        self,
        fleet_mode:   str,
        capital_pres: bool,
        frozen_count: int,
    ) -> str:
        if fleet_mode == "survival":
            return "SURVIVAL MODE: exposicao minima em toda a frota. Revisar causa raiz urgentemente."
        if fleet_mode == "emergency":
            return "EMERGENCY MODE: exposicao reduzida a 35%. Aguardar normalizacao de drift antes de ampliar."
        if capital_pres:
            return "Capital preservation ativo: sizing reduzido automaticamente. Monitorar drift e fleet health."
        if frozen_count > 0:
            return f"{frozen_count} estrategia(s) congelada(s). Revisar antes de reativar."
        if fleet_mode == "throttled":
            return "Throttle ativo: sinais de risco moderados. Sizing conservador ate normalizacao."
        return "Condicoes normais. Execucao autonoma dentro dos parametros esperados."

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist_decision(self, decision: ExecutionDecision) -> None:
        try:
            self.execution_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "decided_at":                 decision.decided_at,
                "decision_id":                decision.decision_id,
                "strategy_id":                decision.strategy_id,
                "activation_state":           decision.activation_state,
                "control_mode":               decision.control_mode,
                "final_sizing":               decision.final_sizing,
                "allocation_weight":          decision.allocation_weight,
                "execution_confidence_score": decision.execution_confidence_score,
                "capital_preservation_active": decision.capital_preservation_active,
                "justification":              decision.justification,
            }
            with open(self.execution_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _empty_report(self) -> ExecutionIntelligenceReport:
        return ExecutionIntelligenceReport(
            execution_confidence_score=0.0, sizing_quality_score=0.0,
            capital_efficiency_score=0.0, decisions=[], fleet_control_mode="normal",
            market_drift_score=0.0, fleet_health_avg=100.0,
            strategies_active=0, strategies_throttled=0,
            strategies_frozen=0, strategies_retired=0,
            total_allocated_capital=0.0, capital_preservation_active=False,
            capital_saved_fraction=0.0, dominant_risk=None,
            recommendation="Nenhuma estrategia fornecida. Execute sweep_runner primeiro.",
            warning="PAPER ONLY — Execucao autonoma simulada. Sem alocacao real.",
            computed_at=datetime.now(timezone.utc).isoformat(),
        )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Execution Intelligence — Phase O FASE 8"
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

    engine = AutonomousExecutionIntelligence(current_regime=args.regime)
    report = engine.execute(strategy_ids)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\n{report.warning}")
    print(f"\nAutonomous Execution Intelligence — {len(report.decisions)} estrategias")
    print(f"  execution_confidence: {report.execution_confidence_score:.0f}/100")
    print(f"  sizing_quality:       {report.sizing_quality_score:.0f}/100")
    print(f"  capital_efficiency:   {report.capital_efficiency_score:.0f}/100")
    print(f"  fleet_mode:           {report.fleet_control_mode}")
    print(f"  market_drift:         {report.market_drift_score:.0f}/100")
    print(f"  fleet_health:         {report.fleet_health_avg:.0f}/100")
    print(f"  capital_preservation: {'ATIVO' if report.capital_preservation_active else 'inativo'}")
    print(f"  states:               {report.strategies_active} active / {report.strategies_throttled} throttled / {report.strategies_frozen} frozen / {report.strategies_retired} retired")
    print(f"  total_allocated:      {report.total_allocated_capital:.1%}")
    print(f"  capital_saved:        {report.capital_saved_fraction:.1%} vs. pedido original")
    if report.dominant_risk:
        print(f"  dominant_risk:        {report.dominant_risk}")

    print(f"\n{'Estrategia':<25} {'Estado':<12} {'Modo':<12} {'Sizing':>7} {'Weight':>7} {'Conf':>6}")
    print("-" * 75)
    for d in report.decisions:
        print(
            f"{d.strategy_id:<25} {d.activation_state:<12} {d.control_mode:<12} "
            f"{d.final_sizing:>7.1%} {d.allocation_weight:>7.1%} {d.execution_confidence_score:>6.0f}"
        )
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

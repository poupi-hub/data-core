"""
strategy_activation_engine.py — Phase O FASE 2

Autonomous Strategy Activation Engine.

Permite ativação/desativação automática de estratégias com base em scores
quantitativos, sem necessidade de aprovação humana por decisão individual.

Diferença fundamental de StrategyLifecycleEngine (Phase N):
  lifecycle:   avalia e recomenda estado
  activation:  executa a transição de ativação/desativação + persiste evento + emite métrica

Scores produzidos:
  - auto_activation_score:  confiança para ativar automaticamente (0–100)
  - strategy_runtime_score: qualidade de runtime da estratégia ativa (0–100)
  - strategy_trust_score:   confiabilidade acumulada histórica (0–100)

Estados de ativação (independentes do lifecycle_state):
  - active:    estratégia ativa para paper trading / produção
  - frozen:    congelada por degradação — sem novas posições
  - throttled: exposição limitada por risco moderado
  - retired:   desativada permanentemente desta fase

Persistência:
  data/strategy_activation_log.jsonl — log auditável de todas as ativações

CLI:
  python -m domains.crypto_coin.research.strategy_activation_engine --all
  python -m domains.crypto_coin.research.strategy_activation_engine --strategy trend_following
  python -m domains.crypto_coin.research.strategy_activation_engine --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.strategy_degradation_intelligence import (
    StrategyDegradationIntelligence,
    DegradationFleetAnalyzer,
)
from domains.crypto_coin.research.fragility_intelligence import FragilityIntelligenceAnalyzer
from domains.crypto_coin.research.strategy_lifecycle import StrategyLifecycleEngine

EXPERIMENTS_DIR   = Path("data/experiments")
ACTIVATION_LOG    = Path("data/strategy_activation_log.jsonl")
ACTIVATION_STATE  = Path("data/strategy_activation_state.json")

# Prometheus (optional)
try:
    from api.metrics import (
        strategy_trust_score as _prom_trust,
        autonomous_strategy_switch_total as _prom_switch,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────

ACTIVATION_STATES = ["active", "throttled", "frozen", "retired"]

# Thresholds de transição
ACTIVATE_MIN_HEALTH   = 60.0   # health_score >= 60 para ativar
THROTTLE_RISK_THRESH  = 45.0   # composite_risk >= 45 → throttled
FREEZE_RISK_THRESH    = 62.0   # composite_risk >= 62 → frozen
RETIRE_RISK_THRESH    = 78.0   # composite_risk >= 78 → retired

# Trust score: peso de histórico acumulado
TRUST_PENALTY_FREEZE  = 10.0   # cada freeze reduz trust em 10 pontos
TRUST_RECOVERY_STEP   = 5.0    # cada ciclo saudável recupera 5 pontos


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ActivationEvent:
    """Evento de ativação/desativação gerado pelo engine."""
    event_id:        str
    strategy_id:     str
    from_state:      str
    to_state:        str
    trigger:         str   # auto_risk | auto_health | auto_retire | manual
    trigger_score:   float
    justification:   str
    occurred_at:     str


@dataclass
class StrategyActivationStatus:
    """Estado completo de ativação de uma estratégia."""
    strategy_id:            str
    activation_state:       str   # active | throttled | frozen | retired
    auto_activation_score:  float   # 0–100: confiança para ativar
    strategy_runtime_score: float   # 0–100: qualidade de runtime
    strategy_trust_score:   float   # 0–100: confiabilidade acumulada

    # Drivers
    health_score:      float
    composite_risk:    float
    fragility_score:   float
    freeze_count:      int     # total de vezes congelado historicamente

    # Auditoria
    last_state_change: str | None
    events_count:      int
    signals:           list[str]
    evaluated_at:      str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActivationFleetReport:
    """Relatório de ativação de toda a frota."""
    strategies:        list[StrategyActivationStatus]
    active_count:      int
    throttled_count:   int
    frozen_count:      int
    retired_count:     int
    fleet_trust_avg:   float
    events_generated:  int
    evaluated_at:      str
    warning:           str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strategies"] = [asdict(s) for s in self.strategies]
        return d


# ── Engine ────────────────────────────────────────────────────────────────────

class StrategyActivationEngine:
    """
    FASE 2: Motor autônomo de ativação de estratégias.

    Executa automaticamente transições de estado de ativação com base em
    scores quantitativos. Todo evento é persistido com lineage completo.

    Princípio de segurança:
      - Transições de upgrade (frozen→active) são mais conservadoras que downgrades
      - Trust score acumulado modera transições automáticas
      - Estado `retired` é terminal (nunca auto-revertido)
    """

    def __init__(
        self,
        experiments_dir: Path = EXPERIMENTS_DIR,
        activation_log:  Path = ACTIVATION_LOG,
        activation_state: Path = ACTIVATION_STATE,
    ):
        self.experiments_dir  = experiments_dir
        self.activation_log   = activation_log
        self.activation_state = activation_state
        self.lifecycle_engine = StrategyLifecycleEngine(experiments_dir)

    # ── Public API ─────────────────────────────────────────────────────────────

    def evaluate(self, strategy_id: str) -> StrategyActivationStatus:
        """Avalia e atualiza o estado de ativação de uma estratégia."""
        current_state = self._load_activation_state(strategy_id)
        freeze_count  = self._load_freeze_count(strategy_id)

        # Dados de degradação e fragilidade
        try:
            deg = StrategyDegradationIntelligence(strategy_id, self.experiments_dir).analyze()
            health_score   = deg.strategy_health_score
            composite_risk = deg.composite_risk_score
        except Exception:
            health_score   = 50.0
            composite_risk = 0.0

        try:
            frag = FragilityIntelligenceAnalyzer(strategy_id, self.experiments_dir).analyze()
            fragility_score = frag.fragility_score
        except Exception:
            fragility_score = 0.0

        # ── Determinar novo estado ────────────────────────────────────────────
        new_state, trigger, justification = self._determine_activation_state(
            current_state  = current_state,
            health_score   = health_score,
            composite_risk = composite_risk,
            fragility_score = fragility_score,
            freeze_count   = freeze_count,
        )

        # ── Scores ────────────────────────────────────────────────────────────
        auto_activation_score = self._compute_auto_activation_score(
            health_score, composite_risk, fragility_score, freeze_count
        )
        runtime_score = self._compute_runtime_score(new_state, health_score, composite_risk)
        trust_score   = self._compute_trust_score(health_score, freeze_count)

        # ── Evento de transição ───────────────────────────────────────────────
        events_generated = 0
        signals: list[str] = []

        if new_state != current_state:
            event = ActivationEvent(
                event_id      = str(uuid.uuid4())[:8],
                strategy_id   = strategy_id,
                from_state    = current_state,
                to_state      = new_state,
                trigger       = trigger,
                trigger_score = round(composite_risk if "risk" in trigger else health_score, 1),
                justification = justification,
                occurred_at   = datetime.now(timezone.utc).isoformat(),
            )
            self._persist_event(event)
            self._save_activation_state(strategy_id, new_state)
            if new_state == "frozen":
                self._increment_freeze_count(strategy_id)
            signals.append(f"Transição automática: {current_state} → {new_state} ({justification})")
            events_generated = 1

            # Prometheus
            if _METRICS_AVAILABLE:
                try:
                    _prom_switch.labels(strategy_id=strategy_id, to_state=new_state).inc()
                except Exception:
                    pass

        # Prometheus trust score
        if _METRICS_AVAILABLE:
            try:
                _prom_trust.set(trust_score)
            except Exception:
                pass

        # Alertas adicionais
        if new_state == "frozen":
            signals.append(f"Estratégia CONGELADA — sem novas posições (risk={composite_risk:.0f})")
        if new_state == "retired":
            signals.append("⛔ Estratégia APOSENTADA — exposição zero permanente")
        if fragility_score >= 70 and new_state == "active":
            signals.append(f"Atenção: estratégia ativa com fragilidade alta ({fragility_score:.0f})")

        return StrategyActivationStatus(
            strategy_id            = strategy_id,
            activation_state       = new_state,
            auto_activation_score  = round(auto_activation_score, 1),
            strategy_runtime_score = round(runtime_score, 1),
            strategy_trust_score   = round(trust_score, 1),
            health_score           = round(health_score, 1),
            composite_risk         = round(composite_risk, 1),
            fragility_score        = round(fragility_score, 1),
            freeze_count           = freeze_count + (1 if new_state == "frozen" and current_state != "frozen" else 0),
            last_state_change      = datetime.now(timezone.utc).isoformat() if events_generated else None,
            events_count           = events_generated,
            signals                = signals,
            evaluated_at           = datetime.now(timezone.utc).isoformat(),
        )

    def evaluate_fleet(self) -> ActivationFleetReport:
        """Avalia e atualiza toda a frota de estratégias."""
        strategy_files = list(self.experiments_dir.glob("*.jsonl"))
        strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]

        statuses: list[StrategyActivationStatus] = []
        total_events = 0

        for sid in strategy_ids:
            try:
                status = self.evaluate(sid)
                statuses.append(status)
                total_events += status.events_count
            except Exception as e:
                print(f"[WARN] Erro ao avaliar {sid}: {e}")

        counts = {s: sum(1 for st in statuses if st.activation_state == s) for s in ACTIVATION_STATES}
        fleet_trust = sum(s.strategy_trust_score for s in statuses) / max(len(statuses), 1)

        return ActivationFleetReport(
            strategies      = sorted(statuses, key=lambda s: s.auto_activation_score, reverse=True),
            active_count    = counts["active"],
            throttled_count = counts["throttled"],
            frozen_count    = counts["frozen"],
            retired_count   = counts["retired"],
            fleet_trust_avg = round(fleet_trust, 1),
            events_generated= total_events,
            evaluated_at    = datetime.now(timezone.utc).isoformat(),
            warning         = "⚠️ AUTONOMOUS ENGINE — decisões automáticas dentro de limites quantitativos",
        )

    # ── State transitions ──────────────────────────────────────────────────────

    def _determine_activation_state(
        self,
        current_state:   str,
        health_score:    float,
        composite_risk:  float,
        fragility_score: float,
        freeze_count:    int,
    ) -> tuple[str, str, str]:
        """Determina o novo estado, trigger e justificativa."""

        # Retired é terminal
        if current_state == "retired":
            return "retired", "terminal", "Estado retired é permanente"

        # Aposentar automaticamente se risco extremo
        if composite_risk >= RETIRE_RISK_THRESH:
            return "retired", "auto_retire", f"composite_risk={composite_risk:.0f} ≥ {RETIRE_RISK_THRESH}"

        # Congelar se risco crítico
        if composite_risk >= FREEZE_RISK_THRESH:
            return "frozen", "auto_risk", f"composite_risk={composite_risk:.0f} ≥ {FREEZE_RISK_THRESH}"

        # Throttle se risco moderado
        if composite_risk >= THROTTLE_RISK_THRESH or fragility_score >= 70:
            return "throttled", "auto_risk", (
                f"composite_risk={composite_risk:.0f} ≥ {THROTTLE_RISK_THRESH} "
                f"ou fragility={fragility_score:.0f} ≥ 70"
            )

        # Recovery: frozen → throttled → active
        if current_state == "frozen" and health_score >= ACTIVATE_MIN_HEALTH + 10:
            # Saída mais conservadora do frozen: vai para throttled primeiro
            return "throttled", "auto_health", f"health={health_score:.0f} recuperado"

        if current_state == "throttled" and health_score >= ACTIVATE_MIN_HEALTH and composite_risk < THROTTLE_RISK_THRESH:
            # Trust-gated: histórico de freezes reduz velocidade de recovery
            trust_gate = max(0, freeze_count * 5)  # cada freeze = +5 pts health necessários
            required_health = ACTIVATE_MIN_HEALTH + trust_gate
            if health_score >= required_health:
                return "active", "auto_health", f"health={health_score:.0f} ≥ required={required_health:.0f}"
            return "throttled", "trust_gate", f"freeze_count={freeze_count} exige health≥{required_health:.0f}"

        # Ativar se novo (experimental) e saudável
        if current_state not in ACTIVATION_STATES and health_score >= ACTIVATE_MIN_HEALTH:
            return "active", "auto_health", f"Primeira ativação (health={health_score:.0f})"

        return current_state, "no_change", "Sem alteração necessária"

    # ── Score computations ────────────────────────────────────────────────────

    def _compute_auto_activation_score(
        self, health: float, risk: float, fragility: float, freeze_count: int
    ) -> float:
        base = health * 0.5 + max(0.0, 100.0 - risk) * 0.3 + max(0.0, 100.0 - fragility) * 0.2
        freeze_penalty = min(30.0, freeze_count * TRUST_PENALTY_FREEZE)
        return max(0.0, min(100.0, base - freeze_penalty))

    def _compute_runtime_score(self, state: str, health: float, risk: float) -> float:
        state_multiplier = {"active": 1.0, "throttled": 0.6, "frozen": 0.2, "retired": 0.0}
        m = state_multiplier.get(state, 0.5)
        base = (health * 0.6 + max(0.0, 100.0 - risk) * 0.4) * m
        return max(0.0, min(100.0, base))

    def _compute_trust_score(self, health: float, freeze_count: int) -> float:
        base = min(100.0, health + TRUST_RECOVERY_STEP)
        penalty = min(80.0, freeze_count * TRUST_PENALTY_FREEZE)
        return max(0.0, min(100.0, base - penalty))

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_activation_state(self, strategy_id: str) -> str:
        try:
            if self.activation_state.exists():
                data = json.loads(self.activation_state.read_text())
                return data.get(strategy_id, "active")
        except Exception:
            pass
        return "active"

    def _save_activation_state(self, strategy_id: str, state: str) -> None:
        try:
            self.activation_state.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {}
            if self.activation_state.exists():
                data = json.loads(self.activation_state.read_text())
            data[strategy_id] = state
            self.activation_state.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_freeze_count(self, strategy_id: str) -> int:
        count = 0
        if not self.activation_log.exists():
            return 0
        try:
            with open(self.activation_log) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("strategy_id") == strategy_id and entry.get("to_state") == "frozen":
                        count += 1
        except Exception:
            pass
        return count

    def _increment_freeze_count(self, strategy_id: str) -> None:
        pass  # count is computed from activation_log — no separate counter needed

    def _persist_event(self, event: ActivationEvent) -> None:
        try:
            self.activation_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.activation_log, "a") as f:
                f.write(json.dumps(asdict(event)) + "\n")
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strategy Activation Engine — Phase O FASE 2"
    )
    parser.add_argument("--strategy", help="Estratégia específica")
    parser.add_argument("--all",  action="store_true", help="Avaliar toda a frota")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = StrategyActivationEngine()

    if args.strategy:
        status = engine.evaluate(args.strategy)
        if args.json:
            print(json.dumps(status.to_dict(), indent=2))
        else:
            print(f"\nActivation Engine — {status.strategy_id}")
            print(f"  activation_state:       {status.activation_state}")
            print(f"  auto_activation_score:  {status.auto_activation_score:.0f}/100")
            print(f"  strategy_runtime_score: {status.strategy_runtime_score:.0f}/100")
            print(f"  strategy_trust_score:   {status.strategy_trust_score:.0f}/100")
            print(f"  health: {status.health_score:.0f}  risk: {status.composite_risk:.0f}  fragility: {status.fragility_score:.0f}  freezes: {status.freeze_count}")
            for s in status.signals:
                print(f"  ⚡ {s}")

    elif args.all:
        report = engine.evaluate_fleet()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\nActivation Fleet Report — {len(report.strategies)} estratégias")
            print(f"  ⚠️  {report.warning}")
            print(f"  active={report.active_count} throttled={report.throttled_count} "
                  f"frozen={report.frozen_count} retired={report.retired_count}")
            print(f"  fleet_trust_avg: {report.fleet_trust_avg:.0f}/100")
            print(f"  events_generated: {report.events_generated}")
            print(f"\n{'Estratégia':<25} {'Estado':<12} {'Activation':>10} {'Runtime':>8} {'Trust':>6}")
            print("-" * 70)
            for s in report.strategies:
                print(
                    f"{s.strategy_id:<25} {s.activation_state:<12} "
                    f"{s.auto_activation_score:>10.0f} {s.strategy_runtime_score:>8.0f} "
                    f"{s.strategy_trust_score:>6.0f}"
                )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

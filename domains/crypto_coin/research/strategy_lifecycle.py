"""
strategy_lifecycle.py — Phase N FASE 3

Autonomous Strategy Lifecycle Engine.

Gerencia o ciclo de vida adaptativo de estratégias quantitativas:
  experimental → candidate → validated → degraded → retired

Scores produzidos:
  - lifecycle_state:   estado atual da estratégia
  - promotion_score:   quão pronta está para avançar de estado
  - retirement_score:  urgência de retirada da produção
  - recovery_score:    potencial de recuperação de degradação

Persistência:
  data/strategy_lifecycle.jsonl — histórico de transições com lineage completo.

Princípio anti-duplicação:
  Reutiliza StrategyDegradationIntelligence, FragilityIntelligenceAnalyzer e
  StrategyRanker. NÃO reimplementa scoring ou replay.

CLI:
  python -m domains.crypto_coin.research.strategy_lifecycle --strategy trend_following
  python -m domains.crypto_coin.research.strategy_lifecycle --all
  python -m domains.crypto_coin.research.strategy_lifecycle --promote trend_following
  python -m domains.crypto_coin.research.strategy_lifecycle --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.experiment_tracker import ExperimentTracker
from domains.crypto_coin.research.strategy_degradation_intelligence import (
    StrategyDegradationIntelligence,
)
from domains.crypto_coin.research.fragility_intelligence import FragilityIntelligenceAnalyzer

EXPERIMENTS_DIR    = Path("data/experiments")
LIFECYCLE_FILE     = Path("data/strategy_lifecycle.jsonl")

# Prometheus (optional)
try:
    from api.metrics import (
        strategy_promotions_total as _prom_promotions,
        strategy_retirement_total as _prom_retirements,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────────

# Estados do ciclo de vida
LIFECYCLE_STATES = [
    "experimental",   # recém-criada, sem dados suficientes
    "candidate",      # dados suficientes, score composto razoável
    "validated",      # estável, robusta, baixo risco
    "degraded",       # degradação detectada — watchlist
    "retired",        # retirada de produção
]

# Regras de transição (thresholds)
PROMOTION_MIN_EXPERIMENTS = 10       # mínimo de experimentos para candidate
PROMOTION_MIN_COMPOSITE   = 50.0     # score composto mínimo para validated
DEGRADATION_RISK_THRESHOLD = 55.0    # composite_risk >= 55 → degraded
RETIREMENT_RISK_THRESHOLD  = 72.0    # composite_risk >= 72 → retired candidate
RECOVERY_HEALTH_THRESHOLD  = 65.0    # health_score >= 65 pode sair de degraded


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class LifecycleTransition:
    """Registro de uma transição de estado."""
    transition_id:  str
    strategy_id:    str
    from_state:     str
    to_state:       str
    reason:         str
    trigger_score:  float   # score que desencadeou a transição
    triggered_at:   str


@dataclass
class StrategyLifecycleStatus:
    """Estado atual completo do ciclo de vida de uma estratégia."""
    strategy_id:        str
    lifecycle_state:    str   # experimental|candidate|validated|degraded|retired

    # Scores
    promotion_score:    float  # 0–100: quão pronta para avançar
    retirement_score:   float  # 0–100: urgência de retirada
    recovery_score:     float  # 0–100: potencial de recuperação

    # Dados de suporte
    experiments_count:  int
    composite_risk:     float
    health_score:       float
    fragility_score:    float
    degradation_score:  float

    # Sinais
    signals:            list[str]
    recommended_action: str
    evaluated_at:       str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Engine ────────────────────────────────────────────────────────────────────

class StrategyLifecycleEngine:
    """
    Gerencia o ciclo de vida adaptativo das estratégias.

    O engine:
      1. Avalia scores de degradação e fragilidade
      2. Determina o estado correto do ciclo de vida
      3. Calcula promotion_score, retirement_score, recovery_score
      4. Persiste transições com lineage
      5. Emite métricas Prometheus nas transições
    """

    def __init__(
        self,
        experiments_dir: Path = EXPERIMENTS_DIR,
        lifecycle_file:  Path = LIFECYCLE_FILE,
    ):
        self.experiments_dir = experiments_dir
        self.lifecycle_file  = lifecycle_file

    def evaluate(self, strategy_id: str) -> StrategyLifecycleStatus:
        """Avalia o estado de ciclo de vida de uma estratégia."""

        # Carrega estado atual persistido
        current_state = self._load_state(strategy_id) or "experimental"

        # Dados de degradação
        try:
            deg    = StrategyDegradationIntelligence(strategy_id, self.experiments_dir).analyze()
            experiments_count = deg.experiments_analyzed
            composite_risk    = deg.composite_risk_score
            health_score      = deg.strategy_health_score
            degradation_score = deg.degradation_score
        except Exception:
            experiments_count = 0
            composite_risk    = 0.0
            health_score      = 50.0
            degradation_score = 0.0

        # Dados de fragilidade
        try:
            frag          = FragilityIntelligenceAnalyzer(strategy_id, self.experiments_dir).analyze()
            fragility_score = frag.fragility_score
        except Exception:
            fragility_score = 0.0

        # ── Determinar estado correto ─────────────────────────────────────────
        new_state = self._determine_state(
            current_state     = current_state,
            experiments_count = experiments_count,
            composite_risk    = composite_risk,
            health_score      = health_score,
        )

        # ── Scores ────────────────────────────────────────────────────────────
        promotion_score  = self._compute_promotion_score(
            state=new_state, experiments_count=experiments_count,
            health_score=health_score, composite_risk=composite_risk,
        )
        retirement_score = self._compute_retirement_score(composite_risk, fragility_score)
        recovery_score   = self._compute_recovery_score(
            state=new_state, health_score=health_score, composite_risk=composite_risk,
        )

        # ── Sinais e recomendação ──────────────────────────────────────────────
        signals: list[str] = []
        if new_state != current_state:
            signals.append(f"Transição de estado: {current_state} → {new_state}")
            self._record_transition(strategy_id, current_state, new_state,
                                    f"composite_risk={composite_risk:.0f}, health={health_score:.0f}",
                                    composite_risk)

        if composite_risk >= RETIREMENT_RISK_THRESHOLD:
            signals.append(f"Risco crítico: composite_risk={composite_risk:.0f}")
        if fragility_score >= 70:
            signals.append(f"Fragilidade alta: fragility={fragility_score:.0f}")
        if health_score >= RECOVERY_HEALTH_THRESHOLD and new_state == "degraded":
            signals.append("Recovery possível — health melhorou acima do threshold")

        recommended_action = self._recommend_action(new_state, promotion_score, retirement_score)

        return StrategyLifecycleStatus(
            strategy_id        = strategy_id,
            lifecycle_state    = new_state,
            promotion_score    = round(promotion_score, 1),
            retirement_score   = round(retirement_score, 1),
            recovery_score     = round(recovery_score, 1),
            experiments_count  = experiments_count,
            composite_risk     = round(composite_risk, 1),
            health_score       = round(health_score, 1),
            fragility_score    = round(fragility_score, 1),
            degradation_score  = round(degradation_score, 1),
            signals            = signals,
            recommended_action = recommended_action,
            evaluated_at       = datetime.now(timezone.utc).isoformat(),
        )

    def evaluate_fleet(self) -> list[StrategyLifecycleStatus]:
        """Avalia todos as estratégias registradas."""
        strategy_files = list(self.experiments_dir.glob("*.jsonl"))
        strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]
        results = []
        for sid in strategy_ids:
            try:
                results.append(self.evaluate(sid))
            except Exception as e:
                print(f"[WARN] Erro ao avaliar {sid}: {e}")
        return sorted(results, key=lambda s: s.retirement_score, reverse=True)

    # ── State transitions ──────────────────────────────────────────────────────

    def _determine_state(
        self,
        current_state:     str,
        experiments_count: int,
        composite_risk:    float,
        health_score:      float,
    ) -> str:
        """Determina o estado correto baseado nos scores atuais."""

        # Forçar retired se risco crítico E já era candidate+
        if (composite_risk >= RETIREMENT_RISK_THRESHOLD
                and current_state in ("candidate", "validated", "degraded")):
            if _METRICS_AVAILABLE:
                try:
                    _prom_retirements.labels(strategy_id="fleet").inc()
                except Exception:
                    pass
            return "retired"

        # Degraded se risco acima do threshold
        if composite_risk >= DEGRADATION_RISK_THRESHOLD and current_state in ("candidate", "validated"):
            return "degraded"

        # Recovery: sai de degraded se health melhorou
        if current_state == "degraded" and health_score >= RECOVERY_HEALTH_THRESHOLD:
            return "candidate"

        # Promoção experimental → candidate
        if current_state == "experimental" and experiments_count >= PROMOTION_MIN_EXPERIMENTS:
            if _METRICS_AVAILABLE:
                try:
                    _prom_promotions.labels(strategy_id="fleet").inc()
                except Exception:
                    pass
            return "candidate"

        # Promoção candidate → validated (health alto + risco baixo)
        if (current_state == "candidate"
                and health_score >= PROMOTION_MIN_COMPOSITE
                and composite_risk < DEGRADATION_RISK_THRESHOLD):
            return "validated"

        return current_state

    # ── Score computations ────────────────────────────────────────────────────

    def _compute_promotion_score(
        self,
        state:             str,
        experiments_count: int,
        health_score:      float,
        composite_risk:    float,
    ) -> float:
        """Score 0–100 indicando quão pronta a estratégia está para avançar."""
        if state in ("retired", "validated"):
            return 0.0
        if state == "degraded":
            # Pode se promover de volta ao candidate se health melhorar
            return max(0.0, health_score - RECOVERY_HEALTH_THRESHOLD) * 3.0

        # experimental → candidate
        if state == "experimental":
            exp_score = min(100.0, (experiments_count / PROMOTION_MIN_EXPERIMENTS) * 50.0)
            return exp_score

        # candidate → validated
        health_component = max(0.0, health_score - 50.0) * 2.0
        risk_component   = max(0.0, 55.0 - composite_risk) * 1.5
        return min(100.0, health_component + risk_component)

    def _compute_retirement_score(self, composite_risk: float, fragility_score: float) -> float:
        """Score 0–100 de urgência de retirada."""
        score = composite_risk * 0.6 + fragility_score * 0.4
        return round(min(100.0, max(0.0, score)), 1)

    def _compute_recovery_score(
        self, state: str, health_score: float, composite_risk: float
    ) -> float:
        """Score 0–100 de potencial de recuperação de degradação."""
        if state not in ("degraded", "retired"):
            return 0.0
        health_component = health_score * 0.6
        risk_component   = max(0.0, 100.0 - composite_risk) * 0.4
        return round(min(100.0, health_component + risk_component), 1)

    def _recommend_action(
        self, state: str, promotion_score: float, retirement_score: float
    ) -> str:
        actions = {
            "experimental": "Executar mais experimentos para qualificar estratégia.",
            "candidate":    "Executar sweep ampliado e out-of-sample validation.",
            "validated":    "Monitoramento regular — estratégia estável.",
            "degraded":     "Reduzir exposure. Executar sweep para identificar causa de degradação.",
            "retired":      "Estratégia retirada de produção. Requer rework completo antes de reativar.",
        }
        base = actions.get(state, "Estado desconhecido.")
        if promotion_score >= 80 and state == "candidate":
            base += " Pronta para promoção a validated."
        if retirement_score >= 80 and state != "retired":
            base += " ⚠️ Considerar retirada imediata."
        return base

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_state(self, strategy_id: str) -> str | None:
        """Carrega o estado mais recente do arquivo de lifecycle."""
        if not self.lifecycle_file.exists():
            return None
        current = None
        try:
            with open(self.lifecycle_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("strategy_id") == strategy_id:
                            current = entry.get("to_state")
                    except Exception:
                        pass
        except Exception:
            pass
        return current

    def _record_transition(
        self,
        strategy_id: str,
        from_state:  str,
        to_state:    str,
        reason:      str,
        trigger_score: float,
    ) -> None:
        """Persiste uma transição de estado com lineage."""
        try:
            self.lifecycle_file.parent.mkdir(parents=True, exist_ok=True)
            transition = LifecycleTransition(
                transition_id = str(uuid.uuid4()),
                strategy_id   = strategy_id,
                from_state    = from_state,
                to_state      = to_state,
                reason        = reason,
                trigger_score = round(trigger_score, 1),
                triggered_at  = datetime.now(timezone.utc).isoformat(),
            )
            with open(self.lifecycle_file, "a") as f:
                f.write(json.dumps(asdict(transition)) + "\n")
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strategy Lifecycle Engine — Phase N FASE 3"
    )
    parser.add_argument("--strategy", help="Avaliar estratégia específica")
    parser.add_argument("--all",  action="store_true", help="Avaliar toda a frota")
    parser.add_argument("--json", action="store_true", help="Output em JSON")
    args = parser.parse_args()

    engine = StrategyLifecycleEngine()

    if args.strategy:
        status = engine.evaluate(args.strategy)
        if args.json:
            print(json.dumps(status.to_dict(), indent=2))
        else:
            print(f"\nLifecycle — {status.strategy_id}")
            print(f"  estado:          {status.lifecycle_state}")
            print(f"  promotion_score: {status.promotion_score:.0f}/100")
            print(f"  retirement_score:{status.retirement_score:.0f}/100")
            print(f"  recovery_score:  {status.recovery_score:.0f}/100")
            print(f"  health:          {status.health_score:.0f} | risk: {status.composite_risk:.0f}")
            if status.signals:
                for s in status.signals:
                    print(f"  ⚡ {s}")
            print(f"  → {status.recommended_action}")

    elif args.all:
        fleet = engine.evaluate_fleet()
        if args.json:
            print(json.dumps([s.to_dict() for s in fleet], indent=2))
        else:
            print(f"\nLifecycle Fleet ({len(fleet)} estratégias)")
            print(f"{'Strategy':<25} {'State':<14} {'Retire':>7} {'Promote':>8} {'Health':>7}")
            print("-" * 70)
            for s in fleet:
                print(
                    f"{s.strategy_id:<25} {s.lifecycle_state:<14} "
                    f"{s.retirement_score:>7.0f} {s.promotion_score:>8.0f} "
                    f"{s.health_score:>7.0f}"
                )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

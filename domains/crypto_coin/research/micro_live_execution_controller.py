"""
micro_live_execution_controller.py — Phase Q Q-1

Micro-Live Execution Controller.

Controla a transicao entre modos paper/live e valida cada trade antes
de autorizar execucao real. Fail-safe first: qualquer duvida = paper.

Live States:
  paper          → modo padrao, sem capital real
  live_micro     → micro-live ativo (capital minimo)
  live_frozen    → live congelado, sem novas execucoes
  live_rollback  → migrando de volta para paper

Funcoes principais:
  validate_live_trade()    — verifica pre-conditions por trade
  authorize_execution()    — aprovacao final com lineage
  enforce_micro_capital()  — garante limite de capital
  emergency_freeze()       — congela live imediatamente
  transition_to_paper()    — rollback gracioso para paper

CLI:
  python -m domains.crypto_coin.research.micro_live_execution_controller --status
  python -m domains.crypto_coin.research.micro_live_execution_controller --validate --json
"""

from __future__ import annotations

import json
import uuid
import argparse
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LIVE_STATE_FILE = Path("data/live_execution_state.json")
LIVE_CTRL_LOG   = Path("data/live_execution_controller_log.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import (
        live_capital_exposure_pct as _prom_exposure,
        autonomous_freeze_state   as _prom_freeze,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Live State Constants ───────────────────────────────────────────────────────

LIVE_STATES       = ("paper", "live_micro", "live_frozen", "live_rollback")
DEFAULT_STATE     = "paper"

# Hard limits — nao alteraveis em runtime
MAX_CAPITAL_LIVE_PCT   = 0.010   # 1% do capital total
MAX_RISK_PER_TRADE_PCT = 0.0025  # 0.25% por trade
MIN_GOVERNANCE_HEALTH  = 65.0    # governance_health_score minimo para live
MIN_READINESS_SCORE    = 75.0    # live_readiness_score minimo para live
MAX_RISK_SCORE         = 50.0    # adaptive_risk_score maximo para live
MAX_DIVERGENCE_SCORE   = 40.0    # divergence_score maximo para manter live


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class TradeValidationResult:
    """Resultado de validacao pre-trade."""
    validation_id:     str
    allowed:           bool
    live_state:        str
    rejection_reason:  str | None
    safety_status:     str   # safe | warning | blocked | frozen

    # Inputs recebidos
    confidence:        float
    risk_score:        float
    portfolio_exposure: float
    governance_health: float
    readiness_score:   float

    # Sizing aprovado (0.0 se blocked)
    approved_size_pct: float   # fracao do capital aprovada

    justification:     str
    validated_at:      str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LiveControllerState:
    """Estado persistido do controlador live."""
    live_state:         str    # paper | live_micro | live_frozen | live_rollback
    entered_live_at:    str | None
    frozen_at:          str | None
    rollback_at:        str | None
    total_live_sessions: int
    last_validation_id:  str | None
    capital_ceiling_usd: float   # limite hard em USD (configurado externamente)
    updated_at:          str


# ── Controller ─────────────────────────────────────────────────────────────────

class MicroLiveExecutionController:
    """
    Q-1: Portao de entrada para qualquer execucao live.

    Toda ordem precisa passar por authorize_execution() antes de ser enviada
    ao exchange. Falha de qualquer check = rejeicao imediata.

    PAPER ONLY por padrao. Live requer ativacao explicita.
    """

    def __init__(
        self,
        state_file:   Path = LIVE_STATE_FILE,
        log_file:     Path = LIVE_CTRL_LOG,
        capital_ceiling_usd: float = 200.0,  # micro-live: $200 max
    ):
        self.state_file          = state_file
        self.log_file            = log_file
        self.capital_ceiling_usd = capital_ceiling_usd
        self._state              = self._load_state()

    # ── Public API ─────────────────────────────────────────────────────────────

    def validate_live_trade(
        self,
        confidence:         float,
        risk_score:         float,
        portfolio_exposure: float,
        governance_health:  float,
        readiness_score:    float,
        requested_size_pct: float = MAX_RISK_PER_TRADE_PCT,
    ) -> TradeValidationResult:
        """Valida pre-conditions de um trade live. Retorna resultado detalhado."""
        vid = str(uuid.uuid4())[:10]

        # 1. Estado do controlador
        if self._state.live_state == "paper":
            return self._reject(vid, "live_state=paper — nao em modo live",
                                "blocked", confidence, risk_score, portfolio_exposure,
                                governance_health, readiness_score)

        if self._state.live_state == "live_frozen":
            return self._reject(vid, "live_state=live_frozen — execucoes suspensas",
                                "frozen", confidence, risk_score, portfolio_exposure,
                                governance_health, readiness_score)

        if self._state.live_state == "live_rollback":
            return self._reject(vid, "live_state=live_rollback — retornando para paper",
                                "blocked", confidence, risk_score, portfolio_exposure,
                                governance_health, readiness_score)

        # 2. Readiness minimo
        if readiness_score < MIN_READINESS_SCORE:
            return self._reject(vid,
                f"readiness_score={readiness_score:.0f} < {MIN_READINESS_SCORE}",
                "blocked", confidence, risk_score, portfolio_exposure,
                governance_health, readiness_score)

        # 3. Governance health
        if governance_health < MIN_GOVERNANCE_HEALTH:
            return self._reject(vid,
                f"governance_health={governance_health:.0f} < {MIN_GOVERNANCE_HEALTH}",
                "blocked", confidence, risk_score, portfolio_exposure,
                governance_health, readiness_score)

        # 4. Risk score
        if risk_score > MAX_RISK_SCORE:
            return self._reject(vid,
                f"risk_score={risk_score:.0f} > {MAX_RISK_SCORE}",
                "blocked", confidence, risk_score, portfolio_exposure,
                governance_health, readiness_score)

        # 5. Capital exposure
        if portfolio_exposure > MAX_CAPITAL_LIVE_PCT:
            return self._reject(vid,
                f"portfolio_exposure={portfolio_exposure:.2%} > {MAX_CAPITAL_LIVE_PCT:.2%}",
                "blocked", confidence, risk_score, portfolio_exposure,
                governance_health, readiness_score)

        # 6. Per-trade size cap
        approved_size = min(requested_size_pct, MAX_RISK_PER_TRADE_PCT)

        safety = "safe" if risk_score < 30 else "warning"
        justification = (
            f"live_micro aprovado: conf={confidence:.2f} risk={risk_score:.0f} "
            f"gov={governance_health:.0f} ready={readiness_score:.0f} "
            f"size={approved_size:.3%}"
        )

        result = TradeValidationResult(
            validation_id    = vid,
            allowed          = True,
            live_state       = self._state.live_state,
            rejection_reason = None,
            safety_status    = safety,
            confidence       = confidence,
            risk_score       = risk_score,
            portfolio_exposure = portfolio_exposure,
            governance_health = governance_health,
            readiness_score  = readiness_score,
            approved_size_pct = approved_size,
            justification    = justification,
            validated_at     = datetime.now(timezone.utc).isoformat(),
        )
        self._state.last_validation_id = vid
        self._save_state()
        self._log(result)
        return result

    def authorize_execution(self, validation_result: TradeValidationResult) -> bool:
        """Autorizacao final — requer resultado de validacao aprovado."""
        if not validation_result.allowed:
            return False
        if validation_result.live_state != "live_micro":
            return False
        return True

    def enforce_micro_capital(self, requested_usd: float) -> float:
        """Limita valor em USD ao teto de micro-capital."""
        return min(requested_usd, self.capital_ceiling_usd * MAX_CAPITAL_LIVE_PCT)

    def emergency_freeze(self, reason: str) -> None:
        """Congela live imediatamente. Sem novas execucoes."""
        self._state.live_state = "live_frozen"
        self._state.frozen_at  = datetime.now(timezone.utc).isoformat()
        self._save_state()
        self._log_event("emergency_freeze", reason)
        if _METRICS_AVAILABLE:
            try:
                _prom_freeze.set(1.0)
            except Exception:
                pass

    def transition_to_paper(self, reason: str) -> None:
        """Rollback gracioso para paper trading."""
        self._state.live_state  = "live_rollback"
        self._state.rollback_at = datetime.now(timezone.utc).isoformat()
        self._save_state()
        self._log_event("transition_to_paper", reason)
        # Completa o rollback
        self._state.live_state = "paper"
        self._save_state()

    def activate_live(self) -> bool:
        """Ativa modo live_micro. Requer estado paper."""
        if self._state.live_state != "paper":
            return False
        self._state.live_state       = "live_micro"
        self._state.entered_live_at  = datetime.now(timezone.utc).isoformat()
        self._state.frozen_at        = None
        self._state.rollback_at      = None
        self._state.total_live_sessions += 1
        self._save_state()
        self._log_event("activate_live", "Manual activation")
        if _METRICS_AVAILABLE:
            try:
                _prom_freeze.set(0.0)
            except Exception:
                pass
        return True

    @property
    def live_state(self) -> str:
        return self._state.live_state

    @property
    def is_live(self) -> bool:
        return self._state.live_state == "live_micro"

    # ── Internals ──────────────────────────────────────────────────────────────

    def _reject(
        self, vid: str, reason: str, safety: str,
        conf: float, risk: float, exp: float, gov: float, ready: float,
    ) -> TradeValidationResult:
        result = TradeValidationResult(
            validation_id    = vid, allowed=False, live_state=self._state.live_state,
            rejection_reason = reason, safety_status=safety,
            confidence=conf, risk_score=risk, portfolio_exposure=exp,
            governance_health=gov, readiness_score=ready,
            approved_size_pct=0.0,
            justification    = f"REJEITADO: {reason}",
            validated_at     = datetime.now(timezone.utc).isoformat(),
        )
        self._log(result)
        return result

    def _load_state(self) -> LiveControllerState:
        if self.state_file.exists():
            try:
                d = json.loads(self.state_file.read_text())
                return LiveControllerState(**d)
            except Exception:
                pass
        return LiveControllerState(
            live_state=DEFAULT_STATE, entered_live_at=None,
            frozen_at=None, rollback_at=None, total_live_sessions=0,
            last_validation_id=None,
            capital_ceiling_usd=self.capital_ceiling_usd,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def _save_state(self) -> None:
        try:
            self._state.updated_at = datetime.now(timezone.utc).isoformat()
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(asdict(self._state), indent=2))
        except Exception:
            pass

    def _log(self, result: TradeValidationResult) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a") as f:
                f.write(json.dumps(result.to_dict()) + "\n")
        except Exception:
            pass

    def _log_event(self, event: str, reason: str) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "event": event, "reason": reason, "live_state": self._state.live_state,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Micro-Live Execution Controller — Phase Q Q-1")
    parser.add_argument("--status",   action="store_true")
    parser.add_argument("--validate", action="store_true", help="Simular validacao")
    parser.add_argument("--freeze",   action="store_true")
    parser.add_argument("--activate", action="store_true", help="Ativar modo live_micro")
    parser.add_argument("--to-paper", action="store_true")
    parser.add_argument("--json",     action="store_true")
    args = parser.parse_args()

    ctrl = MicroLiveExecutionController()

    if args.activate:
        ok = ctrl.activate_live()
        print(f"Ativacao live: {'OK' if ok else 'FALHOU (estado atual: ' + ctrl.live_state + ')'}")
        return

    if args.freeze:
        ctrl.emergency_freeze("CLI manual freeze")
        print("Emergency freeze ativado.")
        return

    if args.to_paper:
        ctrl.transition_to_paper("CLI manual rollback")
        print("Rollback para paper completo.")
        return

    if args.validate:
        result = ctrl.validate_live_trade(
            confidence=0.72, risk_score=35.0, portfolio_exposure=0.008,
            governance_health=75.0, readiness_score=80.0,
        )
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            status = "APROVADO" if result.allowed else "REJEITADO"
            print(f"\nValidacao [{status}]")
            print(f"  live_state:   {result.live_state}")
            print(f"  safety:       {result.safety_status}")
            if result.rejection_reason:
                print(f"  razao:        {result.rejection_reason}")
            else:
                print(f"  approved_size:{result.approved_size_pct:.3%}")
            print(f"  justificativa:{result.justification}")
        return

    # Status
    print(f"\nMicro-Live Execution Controller")
    print(f"  live_state:          {ctrl.live_state}")
    print(f"  capital_ceiling_usd: ${ctrl.capital_ceiling_usd:.0f}")
    print(f"  max_capital_live:    {MAX_CAPITAL_LIVE_PCT:.1%}")
    print(f"  max_risk_per_trade:  {MAX_RISK_PER_TRADE_PCT:.2%}")
    print(f"  min_readiness:       {MIN_READINESS_SCORE:.0f}")
    print(f"  min_governance:      {MIN_GOVERNANCE_HEALTH:.0f}")
    state = ctrl._state
    print(f"\n  Sessions:   {state.total_live_sessions}")
    if state.entered_live_at:
        print(f"  Entered:    {state.entered_live_at}")
    if state.frozen_at:
        print(f"  Frozen at:  {state.frozen_at}")


if __name__ == "__main__":
    main()

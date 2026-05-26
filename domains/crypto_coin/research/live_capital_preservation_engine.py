"""
live_capital_preservation_engine.py — Phase Q Q-5

Live Capital Preservation Engine.

Monitora e enforca limites hard de capital durante execucao live.
Preservacao de capital acima de qualquer objetivo de retorno.

Hard limits (inegociaveis):
  - max 1.0% do capital total em exposicao live
  - max 0.25% por trade individual
  - max 2 losses consecutivos → contracao automatica para 50%
  - max 4 losses consecutivos → freeze automatico
  - max daily drawdown 2%
  - max weekly drawdown 4%

Acoes automaticas:
  - contracao_50pct:   losses>=2 → tamanho de ordem × 0.50
  - contracao_25pct:   losses>=3 → tamanho de ordem × 0.25
  - freeze_capital:    losses>=4 → sem novas ordens
  - daily_halt:        daily_drawdown >= 2% → parada do dia
  - weekly_halt:       weekly_drawdown >= 4% → parada da semana

CLI:
  python -m domains.crypto_coin.research.live_capital_preservation_engine
  python -m domains.crypto_coin.research.live_capital_preservation_engine --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

CAPITAL_LOG  = Path("data/live_capital_preservation_log.jsonl")
AUDIT_LOG    = Path("data/live_execution_audit_log.jsonl")

# Prometheus (optional)
try:
    from api.live_metrics import (
        live_drawdown_pct        as _prom_drawdown,
        live_capital_exposure_pct as _prom_exposure,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Hard limits ────────────────────────────────────────────────────────────────

MAX_TOTAL_EXPOSURE_PCT    = 0.010   # 1% do capital total
MAX_PER_TRADE_PCT         = 0.0025  # 0.25% por trade
MAX_CONSECUTIVE_LOSSES_CONTRACT = 2  # 2 losses → contracao 50%
MAX_CONSECUTIVE_LOSSES_FREEZE   = 4  # 4 losses → freeze
MAX_DAILY_DRAWDOWN_PCT    = 0.020   # 2% daily
MAX_WEEKLY_DRAWDOWN_PCT   = 0.040   # 4% weekly

# Sizing multipliers pos-contraction
SIZE_MULT_NORMAL   = 1.00
SIZE_MULT_CONTRACT = 0.50
SIZE_MULT_REDUCED  = 0.25
SIZE_MULT_FROZEN   = 0.00


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class CapitalChecks:
    exposure_within_limit:  bool   # exposicao total <= 1%
    per_trade_within_limit: bool   # tamanho por trade <= 0.25%
    daily_drawdown_ok:      bool   # drawdown diario < 2%
    weekly_drawdown_ok:     bool   # drawdown semanal < 4%
    consecutive_losses_ok:  bool   # losses consecutivos < freeze threshold
    checks_passed:          int
    checks_total:           int

    @property
    def all_passed(self) -> bool:
        return self.checks_passed == self.checks_total


@dataclass
class CapitalPreservationReport:
    """Relatorio de preservacao de capital live."""
    report_id:            str

    # Estado de capital
    current_exposure_pct:  float   # exposicao atual estimada
    daily_drawdown_pct:    float   # drawdown no dia corrente
    weekly_drawdown_pct:   float   # drawdown na semana corrente
    consecutive_losses:    int

    # Sizing aprovado
    approved_size_multiplier: float  # 1.0 | 0.5 | 0.25 | 0.0
    trading_allowed:          bool

    # Acoes ativadas
    contracting:              bool
    daily_halt:               bool
    weekly_halt:              bool
    capital_frozen:           bool

    # Halt reasons
    halt_reason:              str | None

    # Checks
    checks:                   CapitalChecks

    # Limites hard
    max_total_exposure_pct:   float
    max_per_trade_pct:        float
    max_daily_drawdown_pct:   float
    max_weekly_drawdown_pct:  float

    # Samples
    live_executions_analyzed: int
    analysis_window:          int

    recommendation:           str
    evaluated_at:             str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = asdict(self.checks)
        return d


# ── Engine ─────────────────────────────────────────────────────────────────────

class LiveCapitalPreservationEngine:
    """
    Q-5: Enforca limites hard de capital durante execucao live.

    Capital preservation e a prioridade maxima — acima de qualquer
    objetivo de retorno ou sinal de estrategia.
    """

    def __init__(
        self,
        capital_log:       Path  = CAPITAL_LOG,
        audit_log:         Path  = AUDIT_LOG,
        window:            int   = 20,
        total_capital_usd: float = 10000.0,  # capital total estimado
    ):
        self.capital_log       = capital_log
        self.audit_log         = audit_log
        self.window            = window
        self.total_capital_usd = total_capital_usd

    def evaluate(
        self,
        current_exposure_usd: float = 0.0,
        requested_trade_usd:  float = 0.0,
    ) -> CapitalPreservationReport:
        """Avalia estado de preservacao de capital e retorna decisoes."""
        report_id = str(uuid.uuid4())[:10]
        records   = self._load_records()

        # ── Metricas ───────────────────────────────────────────────────────────
        exposure_pct       = current_exposure_usd / max(self.total_capital_usd, 1.0)
        trade_pct          = requested_trade_usd  / max(self.total_capital_usd, 1.0)
        consecutive_losses = self._count_consecutive_losses(records)
        daily_drawdown     = self._compute_drawdown(records, hours=24)
        weekly_drawdown    = self._compute_drawdown(records, hours=168)

        # ── Checks ────────────────────────────────────────────────────────────
        c1 = exposure_pct <= MAX_TOTAL_EXPOSURE_PCT
        c2 = trade_pct    <= MAX_PER_TRADE_PCT or requested_trade_usd == 0.0
        c3 = daily_drawdown  <= MAX_DAILY_DRAWDOWN_PCT
        c4 = weekly_drawdown <= MAX_WEEKLY_DRAWDOWN_PCT
        c5 = consecutive_losses < MAX_CONSECUTIVE_LOSSES_FREEZE

        checks = CapitalChecks(
            exposure_within_limit  = c1,
            per_trade_within_limit = c2,
            daily_drawdown_ok      = c3,
            weekly_drawdown_ok     = c4,
            consecutive_losses_ok  = c5,
            checks_passed          = sum([c1, c2, c3, c4, c5]),
            checks_total           = 5,
        )

        # ── Acoes automaticas ─────────────────────────────────────────────────
        daily_halt   = not c3
        weekly_halt  = not c4
        capital_frozen = (not c5) or (consecutive_losses >= MAX_CONSECUTIVE_LOSSES_FREEZE)

        contracting = (
            consecutive_losses >= MAX_CONSECUTIVE_LOSSES_CONTRACT and
            not capital_frozen
        )

        if capital_frozen or daily_halt or weekly_halt:
            approved_mult  = SIZE_MULT_FROZEN
            trading_allowed = False
        elif consecutive_losses >= 3:
            approved_mult  = SIZE_MULT_REDUCED
            trading_allowed = True
        elif contracting:
            approved_mult  = SIZE_MULT_CONTRACT
            trading_allowed = True
        else:
            approved_mult  = SIZE_MULT_NORMAL
            trading_allowed = True

        # ── Halt reason ───────────────────────────────────────────────────────
        halt_reason: str | None = None
        if not trading_allowed:
            reasons = []
            if capital_frozen:
                reasons.append(f"losses_consecutivos={consecutive_losses}")
            if daily_halt:
                reasons.append(f"daily_drawdown={daily_drawdown:.2%}")
            if weekly_halt:
                reasons.append(f"weekly_drawdown={weekly_drawdown:.2%}")
            halt_reason = " | ".join(reasons)

        recommendation = self._build_recommendation(
            trading_allowed, approved_mult, consecutive_losses,
            daily_drawdown, weekly_drawdown, exposure_pct,
        )

        report = CapitalPreservationReport(
            report_id              = report_id,
            current_exposure_pct   = round(exposure_pct, 6),
            daily_drawdown_pct     = round(daily_drawdown, 6),
            weekly_drawdown_pct    = round(weekly_drawdown, 6),
            consecutive_losses     = consecutive_losses,
            approved_size_multiplier = approved_mult,
            trading_allowed        = trading_allowed,
            contracting            = contracting,
            daily_halt             = daily_halt,
            weekly_halt            = weekly_halt,
            capital_frozen         = capital_frozen,
            halt_reason            = halt_reason,
            checks                 = checks,
            max_total_exposure_pct = MAX_TOTAL_EXPOSURE_PCT,
            max_per_trade_pct      = MAX_PER_TRADE_PCT,
            max_daily_drawdown_pct = MAX_DAILY_DRAWDOWN_PCT,
            max_weekly_drawdown_pct = MAX_WEEKLY_DRAWDOWN_PCT,
            live_executions_analyzed = len(records),
            analysis_window        = self.window,
            recommendation         = recommendation,
            evaluated_at           = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_drawdown.set(daily_drawdown)
                _prom_exposure.set(exposure_pct)
            except Exception:
                pass

        return report

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _count_consecutive_losses(self, records: list[dict]) -> int:
        count = 0
        for r in reversed(records):
            fill = r.get("fill_rate", 1.0)
            slip = r.get("slippage_bps", 0.0)
            if fill < 0.90 or slip > 15.0:
                count += 1
            else:
                break
        return count

    def _compute_drawdown(self, records: list[dict], hours: int) -> float:
        """Estima drawdown como soma de fees pagas em janela de tempo."""
        if not records:
            return 0.0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.get("recorded_at", ""))
                if ts >= cutoff:
                    recent.append(r)
            except Exception:
                pass
        if not recent:
            return 0.0
        total_fees = sum(r.get("fee_usd", 0.0) for r in recent)
        slip_cost  = sum(
            r.get("slippage_bps", 0.0) / 10000.0 *
            r.get("filled_size", 0.0) * r.get("executed_price", 1.0)
            for r in recent
        )
        total_loss = total_fees + slip_cost
        return min(1.0, total_loss / max(self.total_capital_usd, 1.0))

    def _build_recommendation(
        self, trading: bool, mult: float, losses: int,
        daily_dd: float, weekly_dd: float, exposure: float,
    ) -> str:
        if not trading:
            return (
                f"TRADING SUSPENSO: losses={losses} daily_dd={daily_dd:.2%} "
                f"weekly_dd={weekly_dd:.2%}. Capital protegido. "
                "Aguardar reset de contador ou fim do periodo."
            )
        if mult == SIZE_MULT_REDUCED:
            return (
                f"CONTRACAO SEVERA (25%): {losses} losses consecutivos. "
                "Apenas ordens minimas permitidas."
            )
        if mult == SIZE_MULT_CONTRACT:
            return (
                f"CONTRACAO (50%): {losses} losses consecutivos. "
                "Reduzir tamanho de ordens."
            )
        return (
            f"Capital preservado: exposure={exposure:.2%} "
            f"daily_dd={daily_dd:.3%} weekly_dd={weekly_dd:.3%}. "
            "Operacao normal autorizada."
        )

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: CapitalPreservationReport) -> None:
        try:
            self.capital_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":            report.evaluated_at,
                "trading_allowed":         report.trading_allowed,
                "approved_size_multiplier": report.approved_size_multiplier,
                "consecutive_losses":      report.consecutive_losses,
                "daily_drawdown_pct":      report.daily_drawdown_pct,
                "weekly_drawdown_pct":     report.weekly_drawdown_pct,
                "current_exposure_pct":    report.current_exposure_pct,
                "checks_passed":           report.checks.checks_passed,
                "capital_frozen":          report.capital_frozen,
                "daily_halt":              report.daily_halt,
                "weekly_halt":             report.weekly_halt,
            }
            with open(self.capital_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _load_records(self) -> list[dict]:
        if not self.audit_log.exists():
            return []
        records: list[dict] = []
        try:
            with open(self.audit_log) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return records[-self.window:]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live Capital Preservation Engine — Phase Q Q-5"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--exposure", type=float, default=0.0,
                        help="Exposicao atual em USD")
    parser.add_argument("--trade",    type=float, default=0.0,
                        help="Tamanho do trade solicitado em USD")
    args = parser.parse_args()

    engine = LiveCapitalPreservationEngine()
    report = engine.evaluate(
        current_exposure_usd=args.exposure,
        requested_trade_usd=args.trade,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nLive Capital Preservation Engine")
    print(f"  trading_allowed:          {'SIM' if report.trading_allowed else 'NAO'}")
    print(f"  approved_size_multiplier: {report.approved_size_multiplier:.0%}")
    print(f"  consecutive_losses:       {report.consecutive_losses}")
    print(f"  current_exposure:         {report.current_exposure_pct:.3%}")
    print(f"  daily_drawdown:           {report.daily_drawdown_pct:.3%} (max {report.max_daily_drawdown_pct:.1%})")
    print(f"  weekly_drawdown:          {report.weekly_drawdown_pct:.3%} (max {report.max_weekly_drawdown_pct:.1%})")
    print(f"\n  Acoes:")
    print(f"    contracting:   {'SIM' if report.contracting else 'nao'}")
    print(f"    daily_halt:    {'SIM' if report.daily_halt else 'nao'}")
    print(f"    weekly_halt:   {'SIM' if report.weekly_halt else 'nao'}")
    print(f"    capital_frozen:{'SIM' if report.capital_frozen else 'nao'}")
    if report.halt_reason:
        print(f"\n  Halt reason: {report.halt_reason}")
    print(f"\n  Checks ({report.checks.checks_passed}/{report.checks.checks_total}):")
    c = report.checks
    print(f"    exposure_within_limit:   {'OK' if c.exposure_within_limit else 'FAIL'}")
    print(f"    per_trade_within_limit:  {'OK' if c.per_trade_within_limit else 'FAIL'}")
    print(f"    daily_drawdown_ok:       {'OK' if c.daily_drawdown_ok else 'FAIL'}")
    print(f"    weekly_drawdown_ok:      {'OK' if c.weekly_drawdown_ok else 'FAIL'}")
    print(f"    consecutive_losses_ok:   {'OK' if c.consecutive_losses_ok else 'FAIL'}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

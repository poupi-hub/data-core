"""
scenario_runner.py — Phase K FASE 8

Executa backtests em cenários nomeados padronizados:
  bull_market    — tendência de alta sustentada
  bear_market    — queda acentuada / capitulação
  sideways       — mercado lateral com baixa volatilidade
  high_vol       — alta volatilidade intra-dia (ex: news shock)
  news_shock     — movimento brusco em uma janela de 24-48h
  post_halving   — comportamento pós-halving Bitcoin

Cada cenário define:
  - Período aproximado (start_date, end_date) em dados históricos conhecidos BTC/ETH
  - Tags descritivas
  - Contexto narrativo

Princípio anti-duplicação:
  - Reutiliza replay_from_db() de db_replay.py
  - Reutiliza compute_all() de metrics/calc.py
  - Reutiliza ExperimentTracker.record() para persistência
  - Incrementa scenario_runs_total (metrics.py FASE 12)

CLI:
  python -m domains.crypto_coin.research.scenario_runner --list
  python -m domains.crypto_coin.research.scenario_runner \\
    --scenario bull_market \\
    --strategy trend_following \\
    --symbol BTC/USDT \\
    --tf 1h
  python -m domains.crypto_coin.research.scenario_runner \\
    --all-scenarios \\
    --strategy trend_following
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Cenário ────────────────────────────────────────────────────────────────────

@dataclass
class ScenarioDefinition:
    """Define um cenário de mercado nomeado para replay padronizado."""
    name:        str
    description: str
    tags:        list[str]

    # Período histórico de referência (BTC/USDT como padrão)
    # Pode ser None se o usuário quer inferir do próprio banco de dados
    start_date:  str | None   # ISO date, ex: "2021-01-01"
    end_date:    str | None   # ISO date, ex: "2021-04-01"

    # Hint de duração em dias (usado quando start/end não estão disponíveis)
    approx_days: int = 90

    # Notas editoriais para o relatório
    narrative: str = ""


# ── Catálogo de cenários ──────────────────────────────────────────────────────

SCENARIOS: dict[str, ScenarioDefinition] = {
    "bull_market": ScenarioDefinition(
        name="bull_market",
        description="Tendência de alta sustentada — Bitcoin Jan-Mar 2021",
        tags=["trend", "high_volume", "ascending"],
        start_date="2021-01-01",
        end_date="2021-04-14",
        approx_days=103,
        narrative=(
            "Período de alta forte: BTC saiu de ~$29k para ATH ~$64k. "
            "Testa se a estratégia captura tendências sem overfit em bull runs."
        ),
    ),
    "bear_market": ScenarioDefinition(
        name="bear_market",
        description="Queda acentuada e capitulação — Bitcoin Jun-Dec 2022",
        tags=["downtrend", "high_volatility", "capitulation"],
        start_date="2022-06-01",
        end_date="2022-12-31",
        approx_days=213,
        narrative=(
            "Queda de ~$30k para ~$16k com múltiplos short squeezes. "
            "Testa gestão de risco em bear market e stops apertados."
        ),
    ),
    "sideways": ScenarioDefinition(
        name="sideways",
        description="Mercado lateral com baixa volatilidade — Bitcoin Sep-Oct 2023",
        tags=["ranging", "low_volatility", "choppy"],
        start_date="2023-09-01",
        end_date="2023-10-31",
        approx_days=61,
        narrative=(
            "BTC oscilou entre $25k e $28k por semanas. "
            "Testa se a estratégia evita overtrading em mercados sem direção."
        ),
    ),
    "high_vol": ScenarioDefinition(
        name="high_vol",
        description="Alta volatilidade intradiária — Bitcoin Jan 2024 (ETF launch)",
        tags=["high_volatility", "breakout", "news_driven"],
        start_date="2024-01-10",
        end_date="2024-01-31",
        approx_days=21,
        narrative=(
            "Aprovação do ETF Bitcoin à vista nos EUA (11/01/2024). "
            "Volatilidade extrema intradiária com movimentos de ±10% em horas. "
            "Testa robustez em eventos de liquidez extrema."
        ),
    ),
    "news_shock": ScenarioDefinition(
        name="news_shock",
        description="Choque de notícia pontual — FTX collapse (Nov 2022)",
        tags=["news_shock", "crash", "contagion"],
        start_date="2022-11-06",
        end_date="2022-11-18",
        approx_days=12,
        narrative=(
            "Colapso da FTX: BTC caiu de ~$21k para ~$15.7k em 5 dias. "
            "Testa comportamento em crash evento-driven com liquidity crunch."
        ),
    ),
    "post_halving": ScenarioDefinition(
        name="post_halving",
        description="Período pós-halving — Bitcoin Apr-Jul 2024",
        tags=["halving", "supply_shock", "accumulation"],
        start_date="2024-04-20",
        end_date="2024-07-31",
        approx_days=102,
        narrative=(
            "Bitcoin halving em 20/04/2024. "
            "Período clássico de consolidação seguido de gradual acumulação. "
            "Testa persistência de lucro em mercados de recuperação lenta."
        ),
    ),
}


# ── Resultado de cenário ───────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    scenario:    str
    strategy_id: str
    symbol:      str
    timeframe:   str
    metrics:     dict[str, Any]
    candles_used: int
    actual_start: str | None
    actual_end:   str | None
    run_id:       str = ""
    error:        str | None = None
    tags:         list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.error is None and bool(self.metrics)


# ── Runner ────────────────────────────────────────────────────────────────────

class ScenarioRunner:
    """
    Executa backtests em cenários nomeados usando replay_from_db() e
    registra no ExperimentTracker.
    """

    def __init__(self, db_session: Any) -> None:
        self.db = db_session

    def run(
        self,
        scenario_name: str,
        strategy_id:   str,
        symbol:        str = "BTC/USDT",
        timeframe:     str = "1h",
        initial_balance: float = 10_000.0,
        record:        bool = True,
    ) -> ScenarioResult:
        """
        Executa um cenário específico.

        Args:
            scenario_name:   Nome do cenário (ver SCENARIOS dict)
            strategy_id:     ID canônico da estratégia no StrategyRegistry
            symbol:          Par de trading (ex: "BTC/USDT")
            timeframe:       Timeframe (ex: "1h", "15m")
            initial_balance: Capital inicial em USDT
            record:          Se True, persiste no ExperimentTracker

        Returns:
            ScenarioResult com métricas ou error preenchido
        """
        from api import metrics as prom_metrics

        scenario = SCENARIOS.get(scenario_name)
        if not scenario:
            raise ValueError(
                f"Cenário '{scenario_name}' não encontrado. "
                f"Disponíveis: {list(SCENARIOS.keys())}"
            )

        logger.info(
            "Iniciando cenário '%s' — strategy=%s symbol=%s tf=%s",
            scenario_name, strategy_id, symbol, timeframe,
        )

        try:
            result = self._execute_replay(
                scenario=scenario,
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                initial_balance=initial_balance,
            )

            if record and result.success:
                self._record_experiment(result, scenario)

            prom_metrics.scenario_runs_total.labels(
                scenario=scenario_name,
                strategy_id=strategy_id,
                symbol=symbol.replace("/", "_"),
            ).inc()

            return result

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Cenário '%s' falhou para %s/%s: %s",
                scenario_name, strategy_id, symbol, exc,
            )
            return ScenarioResult(
                scenario=scenario_name,
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                metrics={},
                candles_used=0,
                actual_start=scenario.start_date,
                actual_end=scenario.end_date,
                error=str(exc),
                tags=scenario.tags,
            )

    def run_all(
        self,
        strategy_id: str,
        symbol:      str = "BTC/USDT",
        timeframe:   str = "1h",
        **kwargs:    Any,
    ) -> list[ScenarioResult]:
        """Executa todos os cenários definidos para uma estratégia."""
        results = []
        for name in SCENARIOS:
            result = self.run(name, strategy_id, symbol, timeframe, **kwargs)
            results.append(result)
        return results

    def _execute_replay(
        self,
        scenario:    ScenarioDefinition,
        strategy_id: str,
        symbol:      str,
        timeframe:   str,
        initial_balance: float,
    ) -> ScenarioResult:
        from domains.crypto_coin.backtesting.db_replay import replay_from_db
        from domains.crypto_coin.analytics.metrics.calc import compute_all
        from domains.crypto_coin.research.strategy_registry import get_registry

        registry = get_registry()
        entry    = registry.get(strategy_id)
        if not entry:
            raise ValueError(f"Estratégia '{strategy_id}' não encontrada no registry")

        strategy_params = entry.parameters.copy()

        # Calcular days a partir de start/end quando disponível
        days = scenario.approx_days
        if scenario.start_date and scenario.end_date:
            start = datetime.fromisoformat(scenario.start_date)
            end   = datetime.fromisoformat(scenario.end_date)
            days  = max(1, (end - start).days)

        trades, candles_count, actual_start, actual_end = replay_from_db(
            db=self.db,
            symbol=symbol,
            timeframe=timeframe,
            days=days,
            initial_balance=initial_balance,
            strategy_params=strategy_params,
        )

        metrics = compute_all(trades, initial_balance) if trades else {}

        return ScenarioResult(
            scenario=scenario.name,
            strategy_id=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            metrics=metrics,
            candles_used=candles_count,
            actual_start=str(actual_start) if actual_start else scenario.start_date,
            actual_end=str(actual_end)   if actual_end   else scenario.end_date,
            tags=scenario.tags,
        )

    def _record_experiment(
        self,
        result:   ScenarioResult,
        scenario: ScenarioDefinition,
    ) -> None:
        from domains.crypto_coin.research.experiment_tracker import get_tracker, ExperimentRecord
        from api import metrics as prom_metrics

        tracker = get_tracker()
        record  = ExperimentRecord(
            strategy_id=result.strategy_id,
            strategy_version="scenario",
            symbol=result.symbol,
            timeframe=result.timeframe,
            parameters={"scenario": result.scenario, "tags": scenario.tags},
            metrics=result.metrics,
            replay_dataset="db",
            replay_days=scenario.approx_days,
            replay_start=result.actual_start,
            replay_end=result.actual_end,
            candles_count=result.candles_used,
            notes=f"Scenario: {scenario.name} — {scenario.description}",
        )
        tracker.record(record)
        result.run_id = record.run_id

        prom_metrics.experiment_records_total.labels(
            strategy_id=result.strategy_id,
            replay_dataset="db",
        ).inc()


# ── Relatório ─────────────────────────────────────────────────────────────────

def print_scenario_report(results: list[ScenarioResult]) -> None:
    """Imprime relatório comparativo de cenários no terminal."""
    print("\n" + "="*70)
    print("SCENARIO COMPARISON REPORT")
    print("="*70)

    for r in results:
        status = "✅" if r.success else "❌"
        print(f"\n{status} {r.scenario.upper()} — {r.strategy_id} / {r.symbol} {r.timeframe}")

        if r.error:
            print(f"   Error: {r.error}")
            continue

        m = r.metrics
        if not m:
            print("   Sem trades executados neste cenário.")
            continue

        print(f"   Candles:       {r.candles_used:>6,}")
        print(f"   Trades:        {m.get('total_trades', 0):>6}")
        print(f"   Return:        {m.get('total_return_pct', 0):>+.2f}%")
        print(f"   Sharpe:        {m.get('sharpe') or '—':>8}")
        print(f"   Sortino:       {m.get('sortino') or '—':>8}")
        print(f"   Calmar:        {m.get('calmar') or '—':>8}")
        print(f"   Max DD:        {m.get('max_drawdown', 0):>+.2f}%")
        print(f"   Profit Factor: {m.get('profit_factor') or '—':>8}")
        if r.tags:
            print(f"   Tags: {', '.join(r.tags)}")

    print("\n" + "="*70 + "\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crypto Scenario Runner — Phase K FASE 8")
    p.add_argument("--list",          action="store_true", help="Lista cenários disponíveis")
    p.add_argument("--scenario",      type=str, help="Nome do cenário a executar")
    p.add_argument("--all-scenarios", action="store_true", help="Executa todos os cenários")
    p.add_argument("--strategy",      type=str, default="trend_following", help="ID da estratégia")
    p.add_argument("--symbol",        type=str, default="BTC/USDT", help="Par de trading")
    p.add_argument("--tf",            type=str, default="1h",       help="Timeframe")
    p.add_argument("--balance",       type=float, default=10_000.0, help="Capital inicial USDT")
    p.add_argument("--no-record",     action="store_true", help="Não persiste no ExperimentTracker")
    return p


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    args = _build_parser().parse_args()

    if args.list:
        print("\nCenários disponíveis:")
        for name, s in SCENARIOS.items():
            print(f"  {name:<20} {s.description}")
            print(f"  {'':20} Tags: {', '.join(s.tags)}")
            if s.start_date:
                print(f"  {'':20} Período: {s.start_date} → {s.end_date} (~{s.approx_days}d)")
            print()
        sys.exit(0)

    if not args.scenario and not args.all_scenarios:
        print("Use --list para ver cenários, ou --scenario <name> / --all-scenarios")
        sys.exit(1)

    # Inicializar DB (reutiliza padrão do projeto)
    from database.connection import get_session
    db = next(get_session())

    runner = ScenarioRunner(db)

    if args.all_scenarios:
        results = runner.run_all(
            strategy_id=args.strategy,
            symbol=args.symbol,
            timeframe=args.tf,
            initial_balance=args.balance,
            record=not args.no_record,
        )
    else:
        results = [runner.run(
            scenario_name=args.scenario,
            strategy_id=args.strategy,
            symbol=args.symbol,
            timeframe=args.tf,
            initial_balance=args.balance,
            record=not args.no_record,
        )]

    print_scenario_report(results)

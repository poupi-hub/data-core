"""
sweep_runner.py — Phase H Fase 8

Orquestrador de pesquisa: parameter sweep + batch replay + comparativo.

Permite:
  - Sweep de parâmetros de uma estratégia (grid search ou lista de configs)
  - Batch replay em múltiplos símbolos/timeframes
  - Comparativo automático com registro no ExperimentTracker
  - Relatório de melhores configurações

Reutiliza (anti-duplicação):
  - db_replay.replay_from_db()     — motor de replay offline
  - calc.compute_all()             — métricas financeiras
  - experiment_tracker.record()    — persistência de resultados
  - strategy_registry.get()        — parâmetros canônicos

Princípio: o sweep NUNCA modifica a estratégia canônica.
  Os parâmetros testados são overrides temporários.

CLI:
  python -m domains.crypto_coin.research.sweep_runner \\
    --strategy trend_following \\
    --symbol BTC/USDT \\
    --tf 15m \\
    --days 90 \\
    --sweep rsi_oversold:25,30,35,40 stop_loss_pct:1.5,2.0,2.5

  python -m domains.crypto_coin.research.sweep_runner \\
    --strategy trend_following \\
    --all-symbols \\
    --tf 15m \\
    --days 60
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterator

from domains.crypto_coin.research.experiment_tracker import get_tracker
from domains.crypto_coin.research.strategy_registry  import get_registry

logger = logging.getLogger(__name__)

# ── Symbols / timeframes disponíveis ─────────────────────────────────────────

DEFAULT_SYMBOLS    = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "ADA/USDT"]
DEFAULT_TIMEFRAMES = ["15m", "1h"]


# ── Parameter sweep utilities ─────────────────────────────────────────────────

def parse_sweep_spec(specs: list[str]) -> dict[str, list[Any]]:
    """
    Converte especificações de sweep em dict de valores.

    Formato: "param_name:val1,val2,val3"
    Exemplo: ["rsi_oversold:25,30,35", "stop_loss_pct:1.5,2.0"]
    """
    result: dict[str, list[Any]] = {}
    for spec in specs:
        if ":" not in spec:
            raise ValueError(f"Especificação de sweep inválida: '{spec}'. Use 'param:val1,val2'")
        param, values_str = spec.split(":", 1)
        values = []
        for v in values_str.split(","):
            v = v.strip()
            try:
                values.append(int(v))
            except ValueError:
                try:
                    values.append(float(v))
                except ValueError:
                    values.append(v)
        result[param] = values
    return result


def build_parameter_grid(base_params: dict, sweep: dict[str, list]) -> Iterator[dict]:
    """
    Gera todas as combinações de parâmetros (produto cartesiano).

    Exemplo:
      base_params = {"rsi_period": 14, "stop_loss_pct": 2.0}
      sweep = {"rsi_oversold": [25, 30, 35]}
      → 3 configurações
    """
    if not sweep:
        yield base_params.copy()
        return

    keys   = list(sweep.keys())
    values = [sweep[k] for k in keys]

    for combo in itertools.product(*values):
        params = base_params.copy()
        for key, val in zip(keys, combo):
            params[key] = val
        yield params


# ── Sweep runner ─────────────────────────────────────────────────────────────

def run_sweep(
    db,
    strategy_id:  str,
    symbol:       str,
    timeframe:    str,
    days:         int = 90,
    sweep_params: dict[str, list] | None = None,
    max_configs:  int = 50,
    notes:        str = "",
) -> list[dict[str, Any]]:
    """
    Executa um sweep de parâmetros para uma estratégia/símbolo/timeframe.

    Args:
        db:           Banco de dados (PostgreSQL ou compatível)
        strategy_id:  ID da estratégia no registry
        symbol:       Par de trading (ex: "BTC/USDT")
        timeframe:    Timeframe (ex: "15m")
        days:         Dias de histórico para replay
        sweep_params: Dict {param: [val1, val2, ...]} para variar
        max_configs:  Limite de configurações (evita explosão combinatória)
        notes:        Nota livre para registrar no ExperimentTracker

    Returns:
        Lista de resultados ordenados por Sharpe decrescente.
    """
    # Importação lazy — evita circular import e acelera CLI quando não usado
    from domains.crypto_coin.backtesting.db_replay import replay_from_db

    registry = get_registry()
    tracker  = get_tracker()

    # Carrega parâmetros canônicos como base
    base_params = registry.get_parameters(strategy_id)
    strategy    = registry.get(strategy_id)

    configs = list(build_parameter_grid(base_params, sweep_params or {}))
    if len(configs) > max_configs:
        logger.warning(
            f"Sweep com {len(configs)} configurações excede max_configs={max_configs}. "
            f"Limitando aos primeiros {max_configs}."
        )
        configs = configs[:max_configs]

    logger.info(
        f"[sweep] strategy={strategy_id} symbol={symbol} tf={timeframe} "
        f"days={days} configs={len(configs)}"
    )

    # Phase K FASE 12 — Prometheus: sweep iniciado
    _emit_sweep_start(strategy_id, symbol, timeframe, len(configs))

    results = []
    for i, params in enumerate(configs, 1):
        logger.info(f"[sweep] Config {i}/{len(configs)}: {_compact_params(params, sweep_params)}")

        try:
            t0 = time.monotonic()
            metrics = replay_from_db(
                db=db,
                symbol=symbol,
                timeframe=timeframe,
                days=days,
                realistic=True,
                strategy_params=params,  # db_replay aceita override de parâmetros
            )
            elapsed = time.monotonic() - t0

            if not metrics:
                logger.warning(f"[sweep] Config {i}: sem métricas (sem candles ou trades)")
                continue

            # Registra no ExperimentTracker
            run_id = tracker.record(
                strategy_id=strategy_id,
                strategy_version=strategy.version,
                symbol=symbol,
                timeframe=timeframe,
                parameters=params,
                metrics=metrics,
                replay_dataset="db",
                replay_days=days,
                candles_count=int(metrics.get("candles_count", 0)),
                notes=notes or f"sweep config {i}/{len(configs)}",
                # Phase K FASE 9: tag automática de sweep
                tags=["sweep"],
                group_id=f"sweep-{strategy_id}-{symbol.replace('/', '_')}-{timeframe}",
            )

            results.append({
                "run_id":          run_id[:8],
                "config_index":    i,
                "params_override": _compact_params(params, sweep_params),
                "elapsed_s":       round(elapsed, 2),
                **{k: round(v, 4) if isinstance(v, float) else v
                   for k, v in metrics.items()},
            })

        except Exception as exc:
            logger.error(f"[sweep] Config {i} falhou: {exc}")

    # Ordenar por Sharpe
    results.sort(key=lambda r: r.get("sharpe", float("-inf")), reverse=True)

    logger.info(
        f"[sweep] Concluído: {len(results)}/{len(configs)} configs válidas. "
        f"Best sharpe: {results[0].get('sharpe', 0):.3f}" if results else "[sweep] Sem resultados"
    )

    return results


def run_batch(
    db,
    strategy_id: str,
    symbols:     list[str] | None = None,
    timeframes:  list[str] | None = None,
    days:        int = 90,
    notes:       str = "",
) -> dict[str, list[dict]]:
    """
    Batch replay: executa o replay para múltiplos símbolos e timeframes.

    Usa parâmetros canônicos (sem sweep).
    Útil para comparar performance cross-symbol da mesma estratégia.

    Returns:
        Dict {symbol: [result_per_timeframe]}
    """
    from domains.crypto_coin.backtesting.db_replay import replay_from_db

    registry = get_registry()
    tracker  = get_tracker()
    params   = registry.get_parameters(strategy_id)
    strategy = registry.get(strategy_id)

    _symbols    = symbols    or DEFAULT_SYMBOLS
    _timeframes = timeframes or DEFAULT_TIMEFRAMES

    logger.info(
        f"[batch] strategy={strategy_id} symbols={_symbols} tfs={_timeframes} days={days}"
    )

    batch_results: dict[str, list[dict]] = {}

    for symbol in _symbols:
        batch_results[symbol] = []
        for tf in _timeframes:
            try:
                metrics = replay_from_db(db=db, symbol=symbol, timeframe=tf, days=days)
                if not metrics:
                    continue

                run_id = tracker.record(
                    strategy_id=strategy_id,
                    strategy_version=strategy.version,
                    symbol=symbol,
                    timeframe=tf,
                    parameters=params,
                    metrics=metrics,
                    replay_dataset="db",
                    replay_days=days,
                    candles_count=int(metrics.get("candles_count", 0)),
                    notes=notes or "batch replay",
                )

                batch_results[symbol].append({
                    "run_id":    run_id[:8],
                    "timeframe": tf,
                    "sharpe":    round(metrics.get("sharpe", 0), 3),
                    "sortino":   round(metrics.get("sortino", 0), 3),
                    "calmar":    round(metrics.get("calmar", 0), 3),
                    "max_drawdown": round(metrics.get("max_drawdown", 0), 3),
                    "total_trades": int(metrics.get("total_trades", 0)),
                    "total_return_pct": round(metrics.get("total_return_pct", 0), 2),
                })

            except Exception as exc:
                logger.error(f"[batch] {symbol} {tf} falhou: {exc}")

    return batch_results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _emit_sweep_start(
    strategy_id: str, symbol: str, timeframe: str, n_configs: int
) -> None:
    """Emite métricas Prometheus de início de sweep (Phase K FASE 12)."""
    try:
        from api import metrics as prom_metrics
        prom_metrics.sweep_runs_total.labels(
            strategy_id=strategy_id,
            symbol=symbol.replace("/", "_"),
            timeframe=timeframe,
        ).inc()
        prom_metrics.sweep_combinations_tested_total.labels(
            strategy_id=strategy_id,
        ).inc(n_configs)
    except Exception:  # noqa: BLE001
        pass  # Prometheus não disponível — não quebrar o sweep


def _compact_params(all_params: dict, sweep_params: dict | None) -> dict:
    """Retorna apenas os parâmetros que variam no sweep."""
    if not sweep_params:
        return {}
    return {k: all_params[k] for k in sweep_params if k in all_params}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Sweep Runner — Parameter sweep & batch replay")
    parser.add_argument("--strategy",     type=str, required=True, help="ID da estratégia")
    parser.add_argument("--symbol",       type=str,                help="Par de trading (ex: BTC/USDT)")
    parser.add_argument("--tf",           type=str, default="15m", help="Timeframe (padrão: 15m)")
    parser.add_argument("--days",         type=int, default=90,    help="Dias de histórico (padrão: 90)")
    parser.add_argument("--sweep",        nargs="+",               help="Sweep: 'param:v1,v2,...' (múltiplos)")
    parser.add_argument("--all-symbols",  action="store_true",     help="Batch em todos os símbolos padrão")
    parser.add_argument("--max-configs",  type=int, default=50,    help="Limite de configurações (padrão: 50)")
    parser.add_argument("--json",         action="store_true",     help="Output em JSON")
    parser.add_argument("--db-url",       type=str,                help="URL do banco (padrão: DATABASE_URL)")
    args = parser.parse_args()

    # Conectar ao banco
    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("Erro: DATABASE_URL não configurada", file=sys.stderr)
        sys.exit(1)

    # Import lazy do conector de banco
    try:
        import psycopg2
        import psycopg2.extras
        db = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
    except ImportError:
        print("Erro: psycopg2 não instalado. Execute: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    try:
        if args.all_symbols:
            results = run_batch(
                db=db,
                strategy_id=args.strategy,
                timeframes=[args.tf],
                days=args.days,
            )
            if args.json:
                print(json.dumps(results, indent=2, default=str))
            else:
                for symbol, runs in results.items():
                    print(f"\n{symbol}:")
                    for r in runs:
                        print(f"  tf={r['timeframe']} sharpe={r['sharpe']:.3f} sortino={r['sortino']:.3f} "
                              f"calmar={r['calmar']:.3f} dd={r['max_drawdown']:.3f} trades={r['total_trades']}")
        else:
            if not args.symbol:
                print("Erro: --symbol é obrigatório sem --all-symbols", file=sys.stderr)
                sys.exit(1)

            sweep_params = parse_sweep_spec(args.sweep) if args.sweep else None
            results = run_sweep(
                db=db,
                strategy_id=args.strategy,
                symbol=args.symbol,
                timeframe=args.tf,
                days=args.days,
                sweep_params=sweep_params,
                max_configs=args.max_configs,
            )

            if args.json:
                print(json.dumps(results, indent=2, default=str))
            else:
                print(f"\nResultados (ordenados por Sharpe — top {min(10, len(results))}):")
                for r in results[:10]:
                    override = r.get("params_override", {})
                    print(f"  [{r['run_id']}] sharpe={r.get('sharpe', 0):.3f} "
                          f"sortino={r.get('sortino', 0):.3f} "
                          f"calmar={r.get('calmar', 0):.3f} "
                          f"dd={r.get('max_drawdown', 0):.3f} "
                          f"trades={r.get('total_trades', 0)} "
                          f"params={json.dumps(override)}")
    finally:
        db.close()


if __name__ == "__main__":
    _main()

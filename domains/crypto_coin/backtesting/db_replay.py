"""
DB Replay — Backtesting a partir de dados históricos armazenados no Postgres.

Diferencia-se do `backtest_runner.py` que busca candles do Binance online:
  - Usa `normalized_market_candles` já coletados pelo data-core pipeline
  - Totalmente offline — sem dependência de rede
  - Reproduzível: mesmos candles → mesmo resultado (consistência temporal garantida)
  - Adequado para CI, validação contínua, comparação de versões de estratégia

Uso:
    from domains.crypto_coin.backtesting.db_replay import replay_from_db
    result = replay_from_db(db, symbol="BTC/USDT", timeframe="15m", days=90)
    print(result)

CLI:
    python -m domains.crypto_coin.backtesting.db_replay --symbol BTC/USDT --tf 15m --days 90
    python -m domains.crypto_coin.backtesting.db_replay --all-symbols --days 30
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.normalization.models import NormalizedMarketCandle
from domains.crypto_coin.analytics.metrics.calc import compute_all as compute_metrics
from domains.crypto_coin.backtesting.simulation import (
    DEFAULT_INITIAL_BALANCE,
    PaperState,
    paper_finalize_open_position,
    paper_process_candle,
)
from domains.crypto_coin.config.settings import Config

# Prometheus metrics — wire G-H-05 (Phase H Fase 11)
# Import lazy para não falhar se a API não estiver disponível no contexto CLI
def _get_metrics():
    try:
        from api.metrics import (
            backtest_runs_total,
            backtest_duration_seconds,
            backtest_candles_processed_total,
        )
        return backtest_runs_total, backtest_duration_seconds, backtest_candles_processed_total
    except Exception:
        return None, None, None


def load_candles_from_db(
    db: Session,
    *,
    symbol: str,
    timeframe: str,
    days: int = 90,
    source: str = "binance",
) -> pd.DataFrame:
    """
    Carrega candles OHLCV do Postgres e retorna DataFrame pronto para backtesting.

    Filtra por source/symbol/timeframe/timestamp e garante ordenação temporal.
    Retorna DataFrame com colunas: open, high, low, close, volume (index=timestamp).
    """
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)

    candles = (
        db.query(NormalizedMarketCandle)
        .filter(
            NormalizedMarketCandle.source == source,
            NormalizedMarketCandle.symbol == symbol,
            NormalizedMarketCandle.timeframe == timeframe,
            NormalizedMarketCandle.timestamp >= since,
        )
        .order_by(NormalizedMarketCandle.timestamp)
        .all()
    )

    if not candles:
        return pd.DataFrame()

    rows = [
        {
            "timestamp": c.timestamp,
            "open":      float(c.open)   if c.open   is not None else None,
            "high":      float(c.high)   if c.high   is not None else None,
            "low":       float(c.low)    if c.low    is not None else None,
            "close":     float(c.close)  if c.close  is not None else None,
            "volume":    float(c.volume) if c.volume is not None else 0.0,
        }
        for c in candles
    ]

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.set_index("timestamp")
    return df


def replay_from_db(
    db: Session,
    *,
    symbol: str,
    timeframe: str,
    days: int = 90,
    source: str = "binance",
    realistic: bool = True,
    cfg: Optional[Config] = None,
    strategy_params: dict | None = None,
) -> dict:
    """
    Executa backtest completo usando candles armazenados no Postgres.

    Parâmetros:
        db          — SQLAlchemy session
        symbol      — par de negociação (ex: "BTC/USDT")
        timeframe   — timeframe dos candles (ex: "15m", "1h")
        days        — janela histórica em dias
        source      — fonte dos candles (padrão: "binance")
        realistic   — True ativa intracandle SL/TP + bar+1 execution
        cfg         — Config de estratégia; usa defaults se None

    Retorna dict com métricas do backtest incluindo:
        sharpe, sortino, max_drawdown, expectancy, profit_factor,
        total_return_pct, bnh_return_pct, total_trades, win_rate_pct,
        candles_used, data_source, period
    """
    import time as _time

    _t0 = _time.monotonic()
    _backtest_runs, _backtest_dur, _backtest_candles = _get_metrics()

    from domains.crypto_coin.config.settings import Config as DefaultConfig

    if cfg is None:
        cfg = DefaultConfig(symbol=symbol, timeframe=timeframe)
    else:
        cfg = cfg
        cfg.symbol    = symbol
        cfg.timeframe = timeframe

    # Apply strategy parameter overrides (used by sweep_runner)
    if strategy_params:
        for k, v in strategy_params.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    df = load_candles_from_db(db, symbol=symbol, timeframe=timeframe,
                               days=days, source=source)

    if df.empty or len(df) < 30:
        # Wire metric for insufficient data runs
        if _backtest_runs is not None:
            try:
                _backtest_runs.labels(symbol=symbol, timeframe=timeframe, mode="db").inc()
                elapsed = _time.monotonic() - _t0
                _backtest_dur.labels(symbol=symbol, timeframe=timeframe, mode="db").observe(elapsed)
            except Exception:
                pass
        return {
            "symbol":    symbol,
            "timeframe": timeframe,
            "error":     f"Dados insuficientes: {len(df)} candles (mínimo 30)",
            "candles_used": len(df),
            "data_source": "db",
        }

    initial = DEFAULT_INITIAL_BALANCE
    state   = PaperState(balance=initial)
    trades:  list[dict] = []

    for i in range(len(df)):
        window = df.iloc[: i + 1]
        state, chunk = paper_process_candle(
            window, cfg, state,
            initial_balance=initial,
            bar_time=window.index[-1],
            min_buy_balance=0.0,
            realistic=realistic,
        )
        trades.extend(chunk)

    last_price = float(df["close"].iloc[-1])
    state, final_chunk = paper_finalize_open_position(state, last_price)
    trades.extend(final_chunk)

    first_price = float(df["close"].iloc[0])
    bnh_return  = ((last_price - first_price) / first_price) * 100

    sells = [t for t in trades if "pnl" in t]
    perf  = compute_metrics(sells, initial)

    final_balance = state.balance
    total_pnl     = sum(t["pnl"] for t in sells)
    win_trades    = [t for t in sells if t["pnl"] > 0]

    period_start = df.index[0]
    period_end   = df.index[-1]

    # ── Wire Prometheus metrics — G-H-05 fix (Phase H Fase 11) ──────────────
    elapsed = _time.monotonic() - _t0
    if _backtest_runs is not None:
        try:
            _backtest_runs.labels(symbol=symbol, timeframe=timeframe, mode="db").inc()
            _backtest_dur.labels(symbol=symbol, timeframe=timeframe, mode="db").observe(elapsed)
            _backtest_candles.labels(symbol=symbol, timeframe=timeframe).inc(len(df))
        except Exception:
            pass

    return {
        "symbol":           symbol,
        "timeframe":        timeframe,
        "data_source":      "db",
        "source_feed":      source,
        "days_requested":   days,
        "candles_used":     len(df),
        "candles_count":    len(df),   # alias para ExperimentTracker
        "period":           f"{period_start.date()} → {period_end.date()}",
        "realistic_mode":   realistic,
        "initial_balance":  initial,
        "final_balance":    round(final_balance, 2),
        "total_return_pct": round((final_balance - initial) / initial * 100, 2),
        "total_pnl":        round(total_pnl, 2),
        "total_trades":     len(sells),
        "winning_trades":   len(win_trades),
        "win_count":        len(win_trades),
        "loss_count":       len(sells) - len(win_trades),
        "win_rate_pct":     round(len(win_trades) / len(sells) * 100, 1) if sells else 0.0,
        "bnh_return_pct":   round(bnh_return, 2),
        "bnh_balance":      round(initial * (1 + bnh_return / 100), 2),
        "elapsed_seconds":  round(elapsed, 2),
        # Métricas de performance
        "sharpe":           perf.get("sharpe"),
        "sortino":          perf.get("sortino"),
        "calmar":           perf.get("calmar"),
        "max_drawdown":     perf.get("max_drawdown"),
        "expectancy":       perf.get("expectancy"),
        "profit_factor":    perf.get("profit_factor"),
        "avg_win":          perf.get("avg_win"),
        "avg_loss":         perf.get("avg_loss"),
    }


def replay_all_symbols(
    db: Session,
    *,
    days: int = 30,
    source: str = "binance",
    realistic: bool = True,
) -> list[dict]:
    """Replay para todos os símbolos/timeframes no banco."""
    from sqlalchemy import distinct

    pairs = (
        db.query(
            distinct(NormalizedMarketCandle.symbol),
            NormalizedMarketCandle.timeframe,
        )
        .filter(NormalizedMarketCandle.source == source)
        .all()
    )

    results = []
    for symbol, timeframe in pairs:
        r = replay_from_db(db, symbol=symbol, timeframe=timeframe,
                           days=days, source=source, realistic=realistic)
        results.append(r)
    return results


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DB Replay Backtest")
    parser.add_argument("--symbol",      default=None)
    parser.add_argument("--tf",          default="15m", dest="timeframe")
    parser.add_argument("--source",      default="binance")
    parser.add_argument("--days",        type=int, default=90)
    parser.add_argument("--all-symbols", action="store_true")
    parser.add_argument("--realistic",   action="store_true", default=True)
    parser.add_argument("--json",        action="store_true")
    parser.add_argument("--save",        action="store_true")
    args = parser.parse_args()

    from database.session import SessionLocal
    db = SessionLocal()
    try:
        if args.all_symbols:
            results = replay_all_symbols(db, days=args.days, source=args.source,
                                         realistic=args.realistic)
        elif args.symbol:
            results = [replay_from_db(db, symbol=args.symbol, timeframe=args.timeframe,
                                       days=args.days, source=args.source,
                                       realistic=args.realistic)]
        else:
            import sys
            print("Use --symbol BTC/USDT ou --all-symbols", file=sys.stderr)
            sys.exit(1)

        if args.json or args.save:
            import pathlib
            output = json.dumps(results, indent=2, default=str)
            if args.save:
                pathlib.Path("logs").mkdir(exist_ok=True)
                path = "logs/db_replay_result.json"
                with open(path, "w") as f:
                    f.write(output)
                print(f"💾 Salvo em {path}")
            else:
                print(output)
        else:
            for r in results:
                if "error" in r:
                    print(f"⚠️  {r['symbol']} [{r['timeframe']}]: {r['error']}")
                    continue
                print(f"\n{'━' * 60}")
                print(f"  {r['symbol']} [{r['timeframe']}]  │  {r.get('period', '')}  │  {r['candles_used']} candles")
                print(f"  Retorno   : {r['total_return_pct']:+.2f}%  │  B&H: {r['bnh_return_pct']:+.2f}%")
                print(f"  Trades    : {r['total_trades']}  │  Win rate: {r['win_rate_pct']}%")
                sharpe  = r.get('sharpe')
                sortino = r.get('sortino')
                max_dd  = r.get('max_drawdown')
                pf      = r.get('profit_factor')
                print(f"  Sharpe    : {sharpe or '—'}  │  Sortino: {sortino or '—'}")
                print(f"  Max DD    : {max_dd or '—'}%  │  Profit Factor: {pf or '—'}")
    finally:
        db.close()

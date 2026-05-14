"""
Backtesting — testa a estratégia em dados históricos

Uso:
    python backtest.py                          # parâmetros do .env, 90 dias
    python backtest.py --days 180 --tf 1h
    python backtest.py --realistic              # intracandle SL/TP + bar+1 execution
    python backtest.py --walk-forward           # split treino/teste, reporta os dois
    python backtest.py --compare                # compara 3 presets lado a lado
"""

import argparse
import asyncio
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from domains.crypto_coin.config.settings import load_config, Config
from domains.crypto_coin.analytics.metrics.calc import compute_all as compute_metrics
from domains.crypto_coin.backtesting.simulation import (
    PaperState,
    DEFAULT_INITIAL_BALANCE,
    paper_finalize_open_position,
    paper_process_candle,
)

DIVIDER = "─" * 60


# ── Fetch ─────────────────────────────────────────────────────

async def fetch_history(symbol: str, timeframe: str, days: int,
                        exchange_id: str = "binance") -> pd.DataFrame:
    cls = getattr(ccxt, exchange_id)
    ex  = cls({"enableRateLimit": True})
    since = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    all_candles = []
    try:
        while True:
            batch = await ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not batch:
                break
            all_candles.extend(batch)
            since = batch[-1][0] + 1
            if len(batch) < 1000:
                break
            await asyncio.sleep(0.5)
    finally:
        await ex.close()

    df = pd.DataFrame(all_candles,
                      columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp")


# ── Engine ────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame, cfg: Config, realistic: bool = False) -> dict:
    """
    Roda o backtest em `df` com os parâmetros de `cfg`.
    realistic=True: intracandle SL/TP + bar+1 execution.
    """
    initial = DEFAULT_INITIAL_BALANCE
    state   = PaperState(balance=initial)
    trades  = []

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

    # B&H benchmark
    first_price  = float(df["close"].iloc[0])
    bnh_return   = ((last_price - first_price) / first_price) * 100
    bnh_balance  = initial * (1 + bnh_return / 100)

    # Métricas
    sells = [t for t in trades if "pnl" in t]
    perf  = compute_metrics(sells, initial)

    final_balance = state.balance
    total_pnl     = sum(t["pnl"] for t in sells)

    return {
        "initial_balance": initial,
        "final_balance":   round(final_balance, 2),
        "total_return_pct": round((final_balance - initial) / initial * 100, 2),
        "total_pnl":       round(total_pnl, 2),
        "total_trades":    len(sells),
        "winning_trades":  len([t for t in sells if t["pnl"] > 0]),
        "win_rate_pct":    round(len([t for t in sells if t["pnl"] > 0]) / len(sells) * 100, 1) if sells else 0,
        "sharpe":          perf.get("sharpe"),
        "max_drawdown":    perf.get("max_drawdown"),
        "expectancy":      perf.get("expectancy"),
        "profit_factor":   perf.get("profit_factor"),
        "avg_win":         perf.get("avg_win"),
        "avg_loss":        perf.get("avg_loss"),
        "bnh_return_pct":  round(bnh_return, 2),
        "bnh_balance":     round(bnh_balance, 2),
        "realistic_mode":  realistic,
        "trades":          trades[-20:],
    }


# ── Presets para --compare ────────────────────────────────────

def make_presets(base: Config) -> list[tuple[str, Config]]:
    """Retorna lista de (nome, config) para comparação."""
    presets = []

    # 1. Atual (do .env)
    presets.append(("Atual (.env)", deepcopy(base)))

    # 2. Conservador: stop mais apertado, TP maior, tamanho menor
    c = deepcopy(base)
    c.stop_loss_pct   = max(2.0, base.stop_loss_pct * 0.6)
    c.take_profit_pct = base.take_profit_pct * 1.3
    c.trade_size_pct  = max(10, base.trade_size_pct - 15)
    presets.append(("Conservador", c))

    # 3. Agressivo: stop largo, TP menor, tamanho maior
    a = deepcopy(base)
    a.stop_loss_pct   = base.stop_loss_pct * 1.5
    a.take_profit_pct = base.take_profit_pct * 0.7
    a.trade_size_pct  = min(80, base.trade_size_pct + 20)
    presets.append(("Agressivo", a))

    return presets


# ── Walk-forward ──────────────────────────────────────────────

def run_walk_forward(df: pd.DataFrame, cfg: Config,
                     realistic: bool, split: float = 0.7) -> dict:
    split_idx = int(len(df) * split)
    df_train  = df.iloc[:split_idx]
    df_test   = df.iloc[split_idx:]
    return {
        "train": run_backtest(df_train, cfg, realistic),
        "test":  run_backtest(df_test,  cfg, realistic),
        "train_period": f"{df_train.index[0].date()} → {df_train.index[-1].date()}",
        "test_period":  f"{df_test.index[0].date()}  → {df_test.index[-1].date()}",
    }


# ── Formatação ────────────────────────────────────────────────

def _color(val, *, good_above=None, bad_above=None, reverse=False):
    """Retorna string com cor ANSI."""
    GREEN, YELLOW, RED, RESET = "\033[92m", "\033[93m", "\033[91m", "\033[0m"
    if val is None:
        return "  —  "
    if good_above is not None:
        color = GREEN if val >= good_above else (YELLOW if val >= 0 else RED)
    elif bad_above is not None:
        color = RED if val >= bad_above else (YELLOW if val >= bad_above * 0.5 else GREEN)
    else:
        color = GREEN if val >= 0 else RED
    if reverse:
        color = RED if val >= (bad_above or 0) else GREEN
    return f"{color}{val}{RESET}"


def print_result(result: dict, label: str = "", period: str = ""):
    r = result
    print(f"\n{'━' * 60}")
    if label:
        print(f"  {label}" + (f"  │  {period}" if period else ""))
        print(f"{'━' * 60}")

    ret  = r['total_return_pct']
    bnh  = r['bnh_return_pct']
    dd   = r['max_drawdown']
    sh   = r['sharpe']
    pf   = r['profit_factor']
    ex   = r['expectancy']
    wr   = r['win_rate_pct']

    GREEN, YELLOW, RED, RESET = "\033[92m", "\033[93m", "\033[91m", "\033[0m"
    def c(val, pos_good=True):
        if val is None: return "—"
        col = (GREEN if val >= 0 else RED) if pos_good else (RED if val >= 0 else GREEN)
        return f"{col}{val:+.2f}{RESET}" if isinstance(val, float) else str(val)

    print(f"  Retorno estratégia : {c(ret)}%   │   B&H: {c(bnh)}%")
    print(f"  Saldo final        : ${r['final_balance']:,.2f}  (inicial: ${r['initial_balance']:,.2f})")
    print(f"  Trades             : {r['total_trades']}  │  Wins: {r['winning_trades']}  │  Win rate: {wr or '—'}%")
    print(f"  Sharpe             : {c(sh)}  │  Max Drawdown: {dd or '—'}%")
    print(f"  Profit Factor      : {pf or '—'}  │  Expectancy: {c(ex)}$/trade")
    if r.get('avg_win') and r.get('avg_loss'):
        print(f"  Avg win/loss       : +${r['avg_win']:.2f} / ${r['avg_loss']:.2f}")
    if r['realistic_mode']:
        print(f"  Modo               : realista (intracandle SL/TP + bar+1)")


def print_comparison(results: list[tuple[str, dict]], df: pd.DataFrame):
    bnh = results[0][1]['bnh_return_pct']
    GREEN, YELLOW, RED, RESET = "\033[92m", "\033[93m", "\033[91m", "\033[0m"

    header = f"{'Estratégia':<18} {'Retorno':>8} {'vs B&H':>8} {'Trades':>7} {'WR%':>6} {'Sharpe':>7} {'MaxDD%':>7} {'PF':>6} {'Exp$':>8}"
    print(f"\n{'━' * 80}")
    print(f"  COMPARAÇÃO DE ESTRATÉGIAS  │  B&H referência: {bnh:+.1f}%")
    print(f"{'━' * 80}")
    print(f"  {header}")
    print(f"  {'─' * 76}")

    for name, r in results:
        ret   = r['total_return_pct']
        vs    = ret - bnh
        wr    = r['win_rate_pct'] or 0
        sh    = r['sharpe']
        dd    = r['max_drawdown']
        pf    = r['profit_factor']
        ex    = r['expectancy']

        rc    = GREEN if ret >= 0 else RED
        vc    = GREEN if vs >= 0 else RED
        shc   = GREEN if sh and sh >= 1 else (YELLOW if sh and sh >= 0.5 else RED)
        ddc   = GREEN if dd and dd <= 5 else (YELLOW if dd and dd <= 15 else RED)

        row = (
            f"  {name:<18}"
            f" {rc}{ret:>+7.1f}%{RESET}"
            f" {vc}{vs:>+7.1f}%{RESET}"
            f" {r['total_trades']:>7}"
            f" {wr:>5.1f}%"
            f" {shc}{(sh or 0):>7.2f}{RESET}"
            f" {ddc}{(dd or 0):>6.1f}%{RESET}"
            f" {(pf or 0):>6.2f}"
            f" {(ex or 0):>+7.2f}"
        )
        print(row)

    # B&H como linha de referência
    print(f"  {'─' * 76}")
    print(f"  {'Buy & Hold':<18} {GREEN}{bnh:>+7.1f}%{RESET} {'0.0%':>8}  {'1':>6}  {'—':>6}  {'—':>7}  {'—':>6}  {'—':>7}  {'—':>8}")
    print(f"{'━' * 80}")


# ── Main ──────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="CryptoBot Backtester v2")
    parser.add_argument("--symbol",       default=None)
    parser.add_argument("--tf",           default=None, dest="timeframe")
    parser.add_argument("--days",         type=int, default=90)
    parser.add_argument("--exchange",     default="binance")
    parser.add_argument("--realistic",    action="store_true",
                        help="Intracandle SL/TP + bar+1 execution")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Split 70/30 treino/teste")
    parser.add_argument("--compare",      action="store_true",
                        help="Compara 3 presets de parâmetros lado a lado")
    parser.add_argument("--save",         action="store_true",
                        help="Salva resultado em logs/backtest_result.json")
    args = parser.parse_args()

    load_dotenv(".env", override=False)
    os.environ.setdefault("PAPER_TRADING", "true")
    cfg = load_config()
    if args.symbol:    cfg.symbol    = args.symbol
    if args.timeframe: cfg.timeframe = args.timeframe

    sym = cfg.symbol
    tf  = cfg.timeframe

    print(f"\n🔍 Buscando {args.days} dias de {sym} [{tf}] na {args.exchange}...")
    df = await fetch_history(sym, tf, args.days, args.exchange)
    period = f"{df.index[0].date()} → {df.index[-1].date()}"
    print(f"   {len(df)} candles  │  {period}")

    # ── Comparação ────────────────────────────────────────────
    if args.compare:
        print("\n⚙️  Rodando comparação de estratégias (realistic=True)...")
        presets = make_presets(cfg)
        results = []
        for name, pcfg in presets:
            print(f"   {name}...")
            r = run_backtest(df, pcfg, realistic=True)
            results.append((name, r))
        print_comparison(results, df)
        if args.save:
            _save({"comparison": [{"name": n, **r} for n, r in results]})
        return

    # ── Walk-forward ──────────────────────────────────────────
    if args.walk_forward:
        print("\n⚙️  Walk-forward (70% treino / 30% teste)...")
        wf = run_walk_forward(df, cfg, args.realistic)
        print_result(wf["train"], label="TREINO", period=wf["train_period"])
        print_result(wf["test"],  label="TESTE (out-of-sample)", period=wf["test_period"])
        if args.save:
            _save(wf)
        return

    # ── Backtest simples ───────────────────────────────────────
    mode = "realista" if args.realistic else "padrão"
    print(f"\n⚙️  Executando backtest ({mode})...")
    result = run_backtest(df, cfg, args.realistic)
    print_result(result, label=f"RESULTADO  │  {sym} {tf}  │  {args.days}d", period=period)
    print()

    if args.save:
        _save(result)


def _save(data: dict):
    import pathlib
    pathlib.Path("logs").mkdir(exist_ok=True)
    path = "logs/backtest_result.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"💾 Salvo em {path}")


def main_entry():
    asyncio.run(main())


if __name__ == "__main__":
    main_entry()

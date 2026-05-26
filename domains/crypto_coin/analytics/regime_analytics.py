"""
regime_analytics.py — Phase H Fase 9

Analytics de regime de mercado para o TradingBot.

Expande a análise de regime além do que está em StorageRepository:
  - Win/loss/exposure/drawdown por regime (BULLISH/BEARISH/NEUTRAL/VOLATILE)
  - Distribuição de confiança por regime
  - Volatilidade por bucket (baixa/média/alta/extrema)
  - Correlação entre regime e performance do trade
  - Transições de regime (quantas vezes mudou)

Fontes de dados (SQLite — StorageRepository):
  - regime_history: symbol, timeframe, timestamp, regime, confidence, atr, hv, adx
  - trade_results:  symbol, strategy_id, entry_timestamp, pnl_pct, regime
  - signal_decisions: accepted, regime, confidence, price

Reutiliza: StorageRepository.fetch_regime_performance() (base)
Complementa: StorageRepository com analytics mais ricos

CLI:
  python -m domains.crypto_coin.analytics.regime_analytics \\
    --symbol BTC/USDT --tf 15m --days 90
  python -m domains.crypto_coin.analytics.regime_analytics \\
    --all --days 30 --json
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Buckets de volatilidade ───────────────────────────────────────────────────

VOLATILITY_BUCKETS = [
    ("low",      0.0,  0.5),    # HV < 0.5%
    ("medium",   0.5,  1.5),    # HV 0.5–1.5%
    ("high",     1.5,  3.0),    # HV 1.5–3.0%
    ("extreme",  3.0, 999.0),   # HV > 3.0%
]

KNOWN_REGIMES = ["BULLISH", "BEARISH", "NEUTRAL", "VOLATILE", "UNKNOWN"]


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class RegimePerformanceDetail:
    """Performance de trades em um regime específico."""
    regime:           str
    total_trades:     int
    win_count:        int
    loss_count:       int
    win_rate:         float
    avg_pnl_pct:      float
    total_pnl_pct:    float
    max_drawdown:     float          # maior sequência de perdas
    avg_confidence:   float          # confiança média dos sinais no regime
    total_candles:    int            # candles observados neste regime
    exposure_pct:     float          # % de candles com posição aberta


@dataclass
class VolatilityBucketStats:
    """Estatísticas de trades por bucket de volatilidade."""
    bucket:        str     # low | medium | high | extreme
    hv_range:      tuple[float, float]
    trade_count:   int
    win_count:     int
    win_rate:      float
    avg_pnl_pct:   float


@dataclass
class RegimeTransition:
    """Transição de regime observada."""
    from_regime:   str
    to_regime:     str
    count:         int
    avg_confidence_before: float
    avg_confidence_after:  float


@dataclass
class RegimeAnalyticsReport:
    """Relatório completo de analytics de regime."""
    symbol:           str
    timeframe:        str
    days_analyzed:    int
    total_candles:    int

    # Performance por regime
    by_regime:        dict[str, RegimePerformanceDetail]

    # Distribuição de candles por regime (% de tempo em cada regime)
    regime_distribution: dict[str, float]

    # Volatilidade por bucket
    by_volatility:    list[VolatilityBucketStats]

    # Transições de regime
    transitions:      list[RegimeTransition]

    # Regime com melhor performance
    best_regime:      str | None
    worst_regime:     str | None

    # Sinal de qualidade: regimes com win_rate > 60% e trades >= 5
    reliable_regimes: list[str]


# ── Analytics engine ─────────────────────────────────────────────────────────

def analyze_regime_performance(
    db,                         # StorageRepository (SQLite)
    symbol:    str,
    timeframe: str,
    days:      int = 90,
) -> RegimeAnalyticsReport:
    """
    Análise completa de performance por regime para symbol/timeframe.

    Combina dados de regime_history, trade_results e signal_decisions
    do StorageRepository para produzir analytics ricos.
    """
    # ── Carrega dados via StorageRepository ──────────────────────────────────
    regime_records = db.fetch_regime_performance(symbol=symbol, timeframe=timeframe)
    trade_records  = db.fetch_recent_trades(symbol=symbol, limit=500)
    signal_records = db.fetch_signal_decisions(symbol=symbol, timeframe=timeframe, limit=500)

    # ── Performance por regime ────────────────────────────────────────────────
    regime_trades: dict[str, list[float]] = defaultdict(list)       # pnl_pct por regime
    regime_conf:   dict[str, list[float]] = defaultdict(list)       # confidence por regime

    for trade in trade_records:
        regime = (trade.get("regime") or "UNKNOWN").upper()
        pnl    = trade.get("pnl_pct") or trade.get("strategy_return_pct") or 0.0
        regime_trades[regime].append(float(pnl))

    for signal in signal_records:
        if not signal.get("accepted"):
            continue
        regime = (signal.get("regime") or "UNKNOWN").upper()
        conf   = signal.get("confidence") or 0
        regime_conf[regime].append(float(conf))

    # ── Distribuição de candles por regime ────────────────────────────────────
    regime_candle_counts: dict[str, int] = defaultdict(int)
    all_regime_hist = regime_records if isinstance(regime_records, list) else []

    for r in all_regime_hist:
        reg = (r.get("regime") or "UNKNOWN").upper()
        regime_candle_counts[reg] += 1

    total_candles = sum(regime_candle_counts.values()) or 1
    regime_distribution = {
        reg: round(count / total_candles, 4)
        for reg, count in regime_candle_counts.items()
    }

    # ── Computa detalhes por regime ───────────────────────────────────────────
    by_regime: dict[str, RegimePerformanceDetail] = {}
    all_regimes_seen = set(list(regime_trades.keys()) + list(regime_candle_counts.keys()))

    for regime in all_regimes_seen:
        pnls     = regime_trades.get(regime, [])
        confs    = regime_conf.get(regime, [])
        candles  = regime_candle_counts.get(regime, 0)

        if not pnls:
            win_count  = 0
            loss_count = 0
            win_rate   = 0.0
            avg_pnl    = 0.0
            total_pnl  = 0.0
            max_dd     = 0.0
        else:
            win_count  = sum(1 for p in pnls if p > 0)
            loss_count = sum(1 for p in pnls if p <= 0)
            win_rate   = round(win_count / len(pnls), 4)
            avg_pnl    = round(sum(pnls) / len(pnls), 4)
            total_pnl  = round(sum(pnls), 4)
            max_dd     = _compute_max_drawdown_sequence(pnls)

        avg_conf    = round(sum(confs) / len(confs), 2) if confs else 0.0
        exposure    = round(len(pnls) / max(candles, 1), 4)

        by_regime[regime] = RegimePerformanceDetail(
            regime=regime,
            total_trades=len(pnls),
            win_count=win_count,
            loss_count=loss_count,
            win_rate=win_rate,
            avg_pnl_pct=avg_pnl,
            total_pnl_pct=total_pnl,
            max_drawdown=max_dd,
            avg_confidence=avg_conf,
            total_candles=candles,
            exposure_pct=exposure,
        )

    # ── Volatilidade por bucket ───────────────────────────────────────────────
    # Agrupa candles de regime_history por HV bucket e analisa trades nessas janelas
    bucket_trades: dict[str, list[float]] = defaultdict(list)

    for r in all_regime_hist:
        hv = float(r.get("hv") or 0.0)
        bucket = _get_volatility_bucket(hv)
        # Aproximação: usa sinal de performance mais próximo
        # (regime_history não tem pnl direto — usado como proxy de contexto)
        bucket_trades[bucket].append(0.0)  # placeholder

    # Para trades com hv disponível no contexto
    for trade in trade_records:
        hv = float(trade.get("hv") or 0.0)
        if hv > 0:
            bucket = _get_volatility_bucket(hv)
            pnl = float(trade.get("pnl_pct") or 0.0)
            bucket_trades[bucket].append(pnl)

    by_volatility: list[VolatilityBucketStats] = []
    for bname, bmin, bmax in VOLATILITY_BUCKETS:
        pnls = bucket_trades.get(bname, [])
        real_pnls = [p for p in pnls if p != 0.0]  # remove placeholders

        by_volatility.append(VolatilityBucketStats(
            bucket=bname,
            hv_range=(bmin, bmax),
            trade_count=len(real_pnls),
            win_count=sum(1 for p in real_pnls if p > 0),
            win_rate=round(sum(1 for p in real_pnls if p > 0) / max(len(real_pnls), 1), 4),
            avg_pnl_pct=round(sum(real_pnls) / max(len(real_pnls), 1), 4),
        ))

    # ── Transições de regime ──────────────────────────────────────────────────
    transitions = _compute_regime_transitions(all_regime_hist)

    # ── Best / worst regime ───────────────────────────────────────────────────
    regimes_with_trades = {r: d for r, d in by_regime.items() if d.total_trades >= 3}
    best_regime  = max(regimes_with_trades, key=lambda r: regimes_with_trades[r].avg_pnl_pct, default=None)
    worst_regime = min(regimes_with_trades, key=lambda r: regimes_with_trades[r].avg_pnl_pct, default=None)

    reliable_regimes = [
        r for r, d in regimes_with_trades.items()
        if d.win_rate >= 0.60 and d.total_trades >= 5
    ]

    return RegimeAnalyticsReport(
        symbol=symbol,
        timeframe=timeframe,
        days_analyzed=days,
        total_candles=total_candles,
        by_regime=by_regime,
        regime_distribution=regime_distribution,
        by_volatility=by_volatility,
        transitions=transitions,
        best_regime=best_regime,
        worst_regime=worst_regime,
        reliable_regimes=reliable_regimes,
    )


def analyze_all_symbols(
    db,
    timeframe: str = "15m",
    days:      int = 90,
) -> dict[str, RegimeAnalyticsReport]:
    """
    Executa análise de regime para todos os símbolos disponíveis no DB.
    """
    # Obtém símbolos únicos do trade_results
    try:
        trades = db.fetch_recent_trades(limit=1000)
        symbols = list({t.get("symbol") for t in trades if t.get("symbol")})
    except Exception:
        symbols = ["BTC/USDT", "ETH/USDT"]

    if not symbols:
        logger.warning("[regime] Nenhum símbolo encontrado no banco")
        return {}

    results = {}
    for symbol in symbols:
        try:
            report = analyze_regime_performance(db, symbol=symbol, timeframe=timeframe, days=days)
            results[symbol] = report
            logger.info(
                f"[regime] {symbol} {timeframe}: "
                f"best={report.best_regime} worst={report.worst_regime} "
                f"reliable={report.reliable_regimes}"
            )
        except Exception as exc:
            logger.error(f"[regime] Falha em {symbol}: {exc}")

    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_volatility_bucket(hv: float) -> str:
    for name, bmin, bmax in VOLATILITY_BUCKETS:
        if bmin <= hv < bmax:
            return name
    return "extreme"


def _compute_max_drawdown_sequence(pnls: list[float]) -> float:
    """Calcula o drawdown máximo de uma sequência de P&Ls."""
    if not pnls:
        return 0.0
    max_dd = 0.0
    running = 0.0
    for p in pnls:
        if p < 0:
            running += abs(p)
            max_dd = max(max_dd, running)
        else:
            running = 0.0
    return round(max_dd, 4)


def _compute_regime_transitions(regime_history: list[dict]) -> list[RegimeTransition]:
    """Conta transições entre regimes consecutivos."""
    if len(regime_history) < 2:
        return []

    # Ordena por timestamp
    sorted_hist = sorted(regime_history, key=lambda r: r.get("timestamp", ""))

    transitions_map: dict[tuple[str, str], list] = defaultdict(list)

    for i in range(1, len(sorted_hist)):
        prev = (sorted_hist[i - 1].get("regime") or "UNKNOWN").upper()
        curr = (sorted_hist[i].get("regime") or "UNKNOWN").upper()

        if prev != curr:  # só conta quando muda
            conf_before = float(sorted_hist[i - 1].get("confidence") or 0)
            conf_after  = float(sorted_hist[i].get("confidence") or 0)
            transitions_map[(prev, curr)].append((conf_before, conf_after))

    result = []
    for (from_r, to_r), confs in transitions_map.items():
        avg_before = round(sum(c[0] for c in confs) / len(confs), 2)
        avg_after  = round(sum(c[1] for c in confs) / len(confs), 2)
        result.append(RegimeTransition(
            from_regime=from_r,
            to_regime=to_r,
            count=len(confs),
            avg_confidence_before=avg_before,
            avg_confidence_after=avg_after,
        ))

    return sorted(result, key=lambda t: t.count, reverse=True)


def _report_to_dict(report: RegimeAnalyticsReport) -> dict[str, Any]:
    """Serializa o report para JSON."""
    return {
        "symbol":       report.symbol,
        "timeframe":    report.timeframe,
        "days_analyzed": report.days_analyzed,
        "total_candles": report.total_candles,
        "best_regime":  report.best_regime,
        "worst_regime": report.worst_regime,
        "reliable_regimes": report.reliable_regimes,
        "regime_distribution": report.regime_distribution,
        "by_regime": {
            regime: {
                "total_trades":   d.total_trades,
                "win_count":      d.win_count,
                "loss_count":     d.loss_count,
                "win_rate":       d.win_rate,
                "avg_pnl_pct":    d.avg_pnl_pct,
                "total_pnl_pct":  d.total_pnl_pct,
                "max_drawdown":   d.max_drawdown,
                "avg_confidence": d.avg_confidence,
                "total_candles":  d.total_candles,
                "exposure_pct":   d.exposure_pct,
            }
            for regime, d in report.by_regime.items()
        },
        "by_volatility": [
            {
                "bucket":       s.bucket,
                "hv_range":     list(s.hv_range),
                "trade_count":  s.trade_count,
                "win_count":    s.win_count,
                "win_rate":     s.win_rate,
                "avg_pnl_pct":  s.avg_pnl_pct,
            }
            for s in report.by_volatility
        ],
        "transitions": [
            {
                "from": t.from_regime,
                "to":   t.to_regime,
                "count": t.count,
                "avg_conf_before": t.avg_confidence_before,
                "avg_conf_after":  t.avg_confidence_after,
            }
            for t in report.transitions
        ],
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse
    import os
    import sys

    from domains.crypto_coin.data.storage.repository import create_storage

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Regime Analytics — Phase H Fase 9")
    parser.add_argument("--symbol",    type=str,                  help="Símbolo (ex: BTC/USDT)")
    parser.add_argument("--tf",        type=str, default="15m",   help="Timeframe (padrão: 15m)")
    parser.add_argument("--days",      type=int, default=90,      help="Dias de histórico (padrão: 90)")
    parser.add_argument("--all",       action="store_true",       help="Analisar todos os símbolos")
    parser.add_argument("--json",      action="store_true",       help="Output em JSON")
    parser.add_argument("--db-url",    type=str,                  help="URL do storage (padrão: STORAGE_URL ou sqlite:///data/bot_storage.sqlite3)")
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("STORAGE_URL", "sqlite:///data/bot_storage.sqlite3")
    db     = create_storage(db_url)

    try:
        if args.all:
            all_reports = analyze_all_symbols(db, timeframe=args.tf, days=args.days)
            if args.json:
                print(json.dumps({s: _report_to_dict(r) for s, r in all_reports.items()}, indent=2, default=str))
            else:
                for symbol, r in all_reports.items():
                    print(f"\n{symbol} ({args.tf}):")
                    print(f"  best={r.best_regime} worst={r.worst_regime} reliable={r.reliable_regimes}")
                    for reg, d in r.by_regime.items():
                        if d.total_trades > 0:
                            print(f"  {reg:<10} trades={d.total_trades} wr={d.win_rate:.2f} avg_pnl={d.avg_pnl_pct:+.2f}% dd={d.max_drawdown:.3f}")
        else:
            if not args.symbol:
                print("Erro: --symbol ou --all é obrigatório", file=sys.stderr)
                sys.exit(1)

            report = analyze_regime_performance(db, symbol=args.symbol, timeframe=args.tf, days=args.days)

            if args.json:
                print(json.dumps(_report_to_dict(report), indent=2, default=str))
            else:
                print(f"\nRegime Analytics — {args.symbol} {args.tf} (últimos {args.days}d)")
                print(f"Total candles: {report.total_candles}")
                print(f"Best regime: {report.best_regime} | Worst: {report.worst_regime}")
                print(f"Reliable regimes (wr≥60%, n≥5): {report.reliable_regimes}")
                print("\nDistribuição:")
                for reg, pct in sorted(report.regime_distribution.items(), key=lambda x: -x[1]):
                    print(f"  {reg:<10} {pct*100:.1f}%")
                print("\nPerformance por regime:")
                for reg, d in sorted(report.by_regime.items(), key=lambda x: -x[1].avg_pnl_pct):
                    print(f"  {reg:<10} trades={d.total_trades:<4} wr={d.win_rate:.2f} "
                          f"avg_pnl={d.avg_pnl_pct:+.2f}% dd={d.max_drawdown:.3f} conf={d.avg_confidence:.0f}")
                if report.transitions:
                    print("\nTransições de regime (top 5):")
                    for t in report.transitions[:5]:
                        print(f"  {t.from_regime} → {t.to_regime}: {t.count}x")
    finally:
        db.close()


if __name__ == "__main__":
    _main()

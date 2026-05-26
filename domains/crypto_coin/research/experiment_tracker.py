"""
experiment_tracker.py — Phase H Fase 6

Rastreamento de experimentos de backtesting/replay.

Registra:
  strategy_id, strategy_version, parameters
  timeframe, replay_dataset, replay_period (start/end)
  full metrics dict (sharpe, sortino, calmar, drawdown, expectancy, etc.)
  equity_curve snapshot
  regime_performance breakdown
  created_at, run_id (uuid)

Persistência: JSON Lines (JSONL) — um experimento por linha.
  Arquivo: data/experiments/{strategy_id}.jsonl  (isolado por estratégia)
  Arquivo global: data/experiments/all_experiments.jsonl

Princípio: sem duplicação — reutiliza compute_all() de calc.py e
replay_from_db() de db_replay.py para métricas. Apenas persiste.

CLI:
  python -m domains.crypto_coin.research.experiment_tracker --list
  python -m domains.crypto_coin.research.experiment_tracker --strategy trend_following
  python -m domains.crypto_coin.research.experiment_tracker --best --metric sharpe
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

EXPERIMENTS_DIR = Path(os.environ.get("EXPERIMENTS_DIR", "data/experiments"))


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ExperimentRecord:
    """
    Resultado completo de um experimento de replay/backtest.

    Campos obrigatórios: strategy_id, symbol, timeframe, parameters, metrics.
    Campos opcionais: equity_curve, regime_performance, notes.

    Phase K FASE 9 — Organization additions:
      tags:          Classificação livre (ex: ["scenario:bull_market", "sweep", "baseline"])
      group_id:      Agrupa experimentos relacionados (ex: "sweep-20260516-trend_following")
      parent_run_id: Rastreia linhagem — qual experimento gerou este (ex: sweep derivado de baseline)
    """
    strategy_id:         str
    strategy_version:    str
    symbol:              str
    timeframe:           str
    parameters:          dict[str, Any]

    # Métricas calculadas (output de compute_all() ou replay_from_db())
    metrics: dict[str, Any]   # sharpe, sortino, calmar, max_drawdown, expectancy, ...

    # Dados do dataset usado
    replay_dataset:  str = "db"   # "db" | "binance" | "csv"
    replay_days:     int = 90
    replay_start:    Optional[str] = None
    replay_end:      Optional[str] = None
    candles_count:   int = 0

    # Snapshots opcionais (pode ser omitido para economizar espaço)
    equity_curve:         list[dict] = field(default_factory=list)
    regime_performance:   dict[str, Any] = field(default_factory=dict)

    # Metadata
    run_id:    str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes:     str = ""

    # Phase K FASE 9 — Organization fields
    tags:          list[str] = field(default_factory=list)
    group_id:      Optional[str] = None   # ex: "sweep-20260516-trend_following"
    parent_run_id: Optional[str] = None   # lineage: gerou este experimento a partir de qual?

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    # ── Convenience accessors ──────────────────────────────────────────────

    @property
    def sharpe(self) -> float:
        return self.metrics.get("sharpe", 0.0)

    @property
    def sortino(self) -> float:
        return self.metrics.get("sortino", 0.0)

    @property
    def calmar(self) -> float:
        return self.metrics.get("calmar", 0.0)

    @property
    def max_drawdown(self) -> float:
        return self.metrics.get("max_drawdown", 0.0)

    @property
    def total_trades(self) -> int:
        return int(self.metrics.get("total_trades", 0))

    @property
    def total_return_pct(self) -> float:
        return self.metrics.get("total_return_pct", 0.0)


# ── Storage ──────────────────────────────────────────────────────────────────

class ExperimentTracker:
    """
    Persiste e consulta experimentos de estratégia.

    Formato: JSON Lines (um JSON por linha) por arquivo de estratégia.
    Thread-safety: append-only, sem locking (suficiente para uso single-process).
    """

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR) -> None:
        self.dir = Path(experiments_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── Write ──────────────────────────────────────────────────────────────

    def save(self, experiment: ExperimentRecord) -> str:
        """
        Persiste um experimento.
        Retorna o run_id.
        """
        record_dict = experiment.to_dict()
        line = json.dumps(record_dict, ensure_ascii=False, default=str)

        # Arquivo por estratégia
        strategy_file = self.dir / f"{experiment.strategy_id}.jsonl"
        with open(strategy_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        # Arquivo global
        global_file = self.dir / "all_experiments.jsonl"
        with open(global_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        return experiment.run_id

    def record(
        self,
        strategy_id:       str,
        strategy_version:  str,
        symbol:            str,
        timeframe:         str,
        parameters:        dict[str, Any],
        metrics:           dict[str, Any],
        replay_dataset:    str = "db",
        replay_days:       int = 90,
        replay_start:      str | None = None,
        replay_end:        str | None = None,
        candles_count:     int = 0,
        equity_curve:      list[dict] | None = None,
        regime_performance: dict[str, Any] | None = None,
        notes:             str = "",
        # Phase K FASE 9 — Organization
        tags:              list[str] | None = None,
        group_id:          str | None = None,
        parent_run_id:     str | None = None,
    ) -> str:
        """
        Conveniência: cria ExperimentRecord e persiste.
        Retorna run_id.
        """
        exp = ExperimentRecord(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=symbol,
            timeframe=timeframe,
            parameters=parameters,
            metrics=metrics,
            replay_dataset=replay_dataset,
            replay_days=replay_days,
            replay_start=replay_start,
            replay_end=replay_end,
            candles_count=candles_count,
            equity_curve=equity_curve or [],
            regime_performance=regime_performance or {},
            notes=notes,
            tags=tags or [],
            group_id=group_id,
            parent_run_id=parent_run_id,
        )
        return self.save(exp)

    # ── Read ───────────────────────────────────────────────────────────────

    def load_all(
        self,
        strategy_id: str | None = None,
        symbol:      str | None = None,
        timeframe:   str | None = None,
        limit:       int | None = None,
        # Phase K FASE 9
        tags:        list[str] | None = None,   # filtrar por tags (AND logic)
        group_id:    str | None = None,          # filtrar por grupo
    ) -> list[ExperimentRecord]:
        """
        Carrega experimentos, opcionalmente filtrados.
        Ordenados por created_at decrescente (mais recente primeiro).
        """
        if strategy_id:
            source_file = self.dir / f"{strategy_id}.jsonl"
        else:
            source_file = self.dir / "all_experiments.jsonl"

        if not source_file.exists():
            return []

        records: list[ExperimentRecord] = []
        with open(source_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    rec = ExperimentRecord.from_dict(data)

                    if symbol    and rec.symbol    != symbol:    continue
                    if timeframe and rec.timeframe != timeframe: continue
                    # Phase K FASE 9: tag and group filters
                    if group_id  and rec.group_id  != group_id:  continue
                    if tags:
                        if not all(t in (rec.tags or []) for t in tags):
                            continue

                    records.append(rec)
                except (json.JSONDecodeError, TypeError):
                    continue

        # Ordenar mais recente primeiro
        records.sort(key=lambda r: r.created_at, reverse=True)

        if limit:
            records = records[:limit]

        return records

    def get_best(
        self,
        strategy_id: str | None = None,
        symbol:      str | None = None,
        timeframe:   str | None = None,
        metric:      str = "sharpe",
    ) -> ExperimentRecord | None:
        """
        Retorna o experimento com o melhor valor da métrica informada.
        """
        records = self.load_all(
            strategy_id=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
        )
        if not records:
            return None

        return max(records, key=lambda r: r.metrics.get(metric, float("-inf")))

    def compare(
        self,
        strategy_id: str | None = None,
        symbol:      str | None = None,
        timeframe:   str | None = None,
        top_n:       int = 5,
        sort_by:     str = "sharpe",
        # Phase K FASE 9
        tags:        list[str] | None = None,
        group_id:    str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Retorna comparativo dos N melhores experimentos.
        Formatado para exibição em CLI ou relatório.
        """
        records = self.load_all(
            strategy_id=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            tags=tags,
            group_id=group_id,
        )
        if not records:
            return []

        records.sort(key=lambda r: r.metrics.get(sort_by, float("-inf")), reverse=True)
        top = records[:top_n]

        result = []
        for r in top:
            result.append({
                "run_id":          r.run_id[:8],
                "strategy_id":     r.strategy_id,
                "strategy_version": r.strategy_version,
                "symbol":          r.symbol,
                "timeframe":       r.timeframe,
                "replay_days":     r.replay_days,
                "total_trades":    r.total_trades,
                "sharpe":          round(r.sharpe, 3),
                "sortino":         round(r.sortino, 3),
                "calmar":          round(r.calmar, 3),
                "max_drawdown":    round(r.max_drawdown, 3),
                "total_return_pct": round(r.total_return_pct, 2),
                "created_at":      r.created_at[:19],
                "notes":           r.notes,
                # Phase K FASE 9
                "tags":            r.tags or [],
                "group_id":        r.group_id,
                "parent_run_id":   r.parent_run_id,
            })

        return result

    def summary(self, strategy_id: str | None = None) -> dict[str, Any]:
        """Sumário estatístico de todos os experimentos."""
        records = self.load_all(strategy_id=strategy_id)
        if not records:
            return {"total": 0}

        sharpe_vals   = [r.sharpe       for r in records]
        sortino_vals  = [r.sortino      for r in records]
        calmar_vals   = [r.calmar       for r in records]
        dd_vals       = [r.max_drawdown for r in records]
        trade_counts  = [r.total_trades for r in records]

        def _avg(lst: list[float]) -> float:
            return round(sum(lst) / len(lst), 3) if lst else 0.0

        def _best(lst: list[float]) -> float:
            return round(max(lst), 3) if lst else 0.0

        return {
            "total":           len(records),
            "strategies":      list({r.strategy_id for r in records}),
            "symbols":         list({r.symbol      for r in records}),
            "timeframes":      list({r.timeframe   for r in records}),
            "avg_sharpe":      _avg(sharpe_vals),
            "best_sharpe":     _best(sharpe_vals),
            "avg_sortino":     _avg(sortino_vals),
            "best_sortino":    _best(sortino_vals),
            "avg_calmar":      _avg(calmar_vals),
            "best_calmar":     _best(calmar_vals),
            "avg_max_drawdown": _avg(dd_vals),
            "avg_trades":      _avg(trade_counts),  # type: ignore[arg-type]
            "latest_run":      records[0].created_at[:19] if records else None,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

_tracker: ExperimentTracker | None = None


def get_tracker(experiments_dir: Path | None = None) -> ExperimentTracker:
    """Retorna instância singleton do tracker."""
    global _tracker
    if _tracker is None:
        _tracker = ExperimentTracker(experiments_dir or EXPERIMENTS_DIR)
    return _tracker


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Experiment Tracker — CLI")
    parser.add_argument("--list",       action="store_true",  help="Listar experimentos")
    parser.add_argument("--strategy",   type=str,             help="Filtrar por strategy_id")
    parser.add_argument("--symbol",     type=str,             help="Filtrar por symbol (ex: BTC/USDT)")
    parser.add_argument("--timeframe",  type=str,             help="Filtrar por timeframe (ex: 15m)")
    parser.add_argument("--best",       action="store_true",  help="Mostrar melhor experimento")
    parser.add_argument("--metric",     type=str, default="sharpe", help="Métrica de comparação (padrão: sharpe)")
    parser.add_argument("--compare",    action="store_true",  help="Comparativo top 5")
    parser.add_argument("--summary",    action="store_true",  help="Sumário estatístico")
    parser.add_argument("--json",       action="store_true",  help="Output em JSON")
    parser.add_argument("--top",        type=int, default=5,  help="Número de resultados (padrão: 5)")
    args = parser.parse_args()

    tracker = get_tracker()

    if args.summary:
        result = tracker.summary(strategy_id=args.strategy)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Total de experimentos: {result.get('total', 0)}")
            print(f"Estratégias: {result.get('strategies', [])}")
            print(f"Avg Sharpe: {result.get('avg_sharpe')} | Best: {result.get('best_sharpe')}")
            print(f"Avg Sortino: {result.get('avg_sortino')} | Best: {result.get('best_sortino')}")
            print(f"Avg Calmar: {result.get('avg_calmar')} | Best: {result.get('best_calmar')}")
        return

    if args.compare or args.list:
        rows = tracker.compare(
            strategy_id=args.strategy,
            symbol=args.symbol,
            timeframe=args.timeframe,
            top_n=args.top,
            sort_by=args.metric,
        )
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                print("Nenhum experimento encontrado.")
                return
            header = f"{'run_id':<10} {'strategy':<20} {'symbol':<12} {'tf':<5} {'sharpe':>7} {'sortino':>8} {'calmar':>7} {'dd':>7} {'trades':>7}"
            print(header)
            print("-" * len(header))
            for r in rows:
                print(f"{r['run_id']:<10} {r['strategy_id']:<20} {r['symbol']:<12} {r['timeframe']:<5} "
                      f"{r['sharpe']:>7.3f} {r['sortino']:>8.3f} {r['calmar']:>7.3f} "
                      f"{r['max_drawdown']:>7.3f} {r['total_trades']:>7}")
        return

    if args.best:
        best = tracker.get_best(
            strategy_id=args.strategy,
            symbol=args.symbol,
            timeframe=args.timeframe,
            metric=args.metric,
        )
        if not best:
            print("Nenhum experimento encontrado.")
            return
        result = best.to_dict()
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Melhor experimento por {args.metric}:")
            print(f"  run_id: {result['run_id']}")
            print(f"  strategy: {result['strategy_id']} v{result['strategy_version']}")
            print(f"  symbol: {result['symbol']} | timeframe: {result['timeframe']}")
            print(f"  sharpe: {best.sharpe:.3f} | sortino: {best.sortino:.3f} | calmar: {best.calmar:.3f}")
            print(f"  max_drawdown: {best.max_drawdown:.3f} | trades: {best.total_trades}")
            print(f"  created_at: {result['created_at'][:19]}")
        return

    parser.print_help()


if __name__ == "__main__":
    _main()

"""
strategy_ranker.py — Phase I Fase 9

Ranking composto de estratégias baseado em múltiplas métricas de performance.

Score composto (0–100):
  - Sharpe Ratio     : 30% (normalizado 0–5)
  - Sortino Ratio    : 20% (normalizado 0–10)
  - Calmar Ratio     : 20% (normalizado 0–5)
  - Max Drawdown     : 15% (menos negativo = melhor, normalizado)
  - Expectancy       : 10% (normalizado 0–200)
  - Consistência     :  5% (% de experimentos com sharpe > 0)

Agregação: melhor experimento por estratégia (por Sharpe) como baseline,
ou média dos top-N se `use_average=True`.

CLI:
    python -m domains.crypto_coin.research.strategy_ranker --top 5
    python -m domains.crypto_coin.research.strategy_ranker --symbol BTC/USDT --tf 15m
    python -m domains.crypto_coin.research.strategy_ranker --json
    python -m domains.crypto_coin.research.strategy_ranker --compare trend_following breakout_scalper
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from typing import Any

from .experiment_tracker import ExperimentTracker
from .strategy_registry  import get_registry


# ── Pesos do score composto ───────────────────────────────────────────────────

WEIGHTS = {
    "sharpe":     0.30,
    "sortino":    0.20,
    "calmar":     0.20,
    "drawdown":   0.15,
    "expectancy": 0.10,
    "consistency":0.05,
}

# Clamp ranges para normalização (valores além do clamp → 0 ou 1)
CLAMP = {
    "sharpe":     (0.0, 5.0),
    "sortino":    (0.0, 10.0),
    "calmar":     (0.0, 5.0),
    "drawdown":   (-80.0, 0.0),  # max_drawdown: -80% = pior, 0% = melhor
    "expectancy": (0.0, 200.0),
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class StrategyRankEntry:
    strategy_id:   str
    experiment_count: int
    # Métricas do melhor experimento (por Sharpe)
    best_sharpe:   float
    best_sortino:  float
    best_calmar:   float
    max_drawdown:  float
    best_expectancy: float
    consistency:   float   # % experimentos com sharpe > 0
    total_trades:  int
    symbol:        str
    timeframe:     str
    run_id:        str
    # Score composto
    composite_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rank":            None,  # preenchido na listagem
            "strategy_id":     self.strategy_id,
            "composite_score": round(self.composite_score, 2),
            "experiment_count": self.experiment_count,
            "consistency_pct": round(self.consistency * 100, 1),
            "best_sharpe":     round(self.best_sharpe, 3),
            "best_sortino":    round(self.best_sortino, 3),
            "best_calmar":     round(self.best_calmar, 3),
            "max_drawdown":    round(self.max_drawdown, 2),
            "best_expectancy": round(self.best_expectancy, 2),
            "total_trades":    self.total_trades,
            "symbol":          self.symbol,
            "timeframe":       self.timeframe,
            "run_id":          self.run_id,
            "score_breakdown": {k: round(v, 3) for k, v in self.score_breakdown.items()},
        }


@dataclass
class RankingReport:
    entries:  list[StrategyRankEntry]
    filters:  dict[str, Any]

    def format_table(self) -> str:
        if not self.entries:
            return "Nenhuma estratégia rankeada."

        header = (
            f"{'#':>3}  {'Strategy':<22}  {'Score':>6}  "
            f"{'Sharpe':>7}  {'Sortino':>7}  {'Drawdown':>9}  "
            f"{'Consist.':>9}  {'Trades':>7}  {'Symbol':<12}  {'TF'}"
        )
        sep = "-" * len(header)
        lines = [header, sep]

        for i, e in enumerate(self.entries, 1):
            lines.append(
                f"{i:>3}  {e.strategy_id:<22}  {e.composite_score:>6.1f}  "
                f"{e.best_sharpe:>7.3f}  {e.best_sortino:>7.3f}  "
                f"{e.max_drawdown:>8.1f}%  "
                f"{e.consistency*100:>8.1f}%  {e.total_trades:>7}  "
                f"{e.symbol:<12}  {e.timeframe}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        result = [e.to_dict() for e in self.entries]
        for i, r in enumerate(result, 1):
            r["rank"] = i
        return {"filters": self.filters, "ranking": result}


# ── Ranker ────────────────────────────────────────────────────────────────────

class StrategyRanker:
    """
    Computa o ranking composto de estratégias a partir dos experimentos registrados.
    """

    def __init__(self) -> None:
        self.tracker  = ExperimentTracker()
        self.registry = get_registry()

    def rank(
        self,
        *,
        symbol:       str | None = None,
        timeframe:    str | None = None,
        strategy_ids: list[str] | None = None,
        top_n:        int = 10,
        min_experiments: int = 1,
    ) -> RankingReport:
        """
        Computa ranking de estratégias.

        Args:
            symbol:       Filtrar por símbolo (ex: 'BTC/USDT')
            timeframe:    Filtrar por timeframe (ex: '15m')
            strategy_ids: Lista de estratégias a comparar (None = todas)
            top_n:        Número de posições no ranking final
            min_experiments: Mínimo de experimentos para incluir no ranking
        """
        filters = {
            "symbol":    symbol,
            "timeframe": timeframe,
            "top_n":     top_n,
            "min_experiments": min_experiments,
        }

        # Descobrir estratégias
        if strategy_ids:
            sids = strategy_ids
        else:
            sids = _discover_strategies()

        entries: list[StrategyRankEntry] = []

        for sid in sids:
            records = self.tracker.load_all(
                strategy_id=sid,
                symbol=symbol,
                timeframe=timeframe,
            )
            if len(records) < min_experiments:
                continue

            entry = self._build_entry(sid, records)
            if entry:
                entries.append(entry)

        # Ordenar por score composto
        entries.sort(key=lambda e: e.composite_score, reverse=True)

        # Phase K FASE 7+12: atualizar scores no Prometheus
        self.update_prometheus_scores(symbol=symbol, timeframe=timeframe)

        return RankingReport(entries=entries[:top_n], filters=filters)

    def compare_head_to_head(
        self,
        strategy_a: str,
        strategy_b: str,
        symbol:     str | None = None,
        timeframe:  str | None = None,
    ) -> dict[str, Any]:
        """
        Phase K FASE 7: Comparação formal head-to-head entre duas estratégias.

        Retorna um dict com:
          - métricas lado a lado para ambas as estratégias
          - winner por cada métrica
          - winner geral (maior composite_score)
          - nota de confiança (baseada em número de experimentos)
        """
        entries_a = self.tracker.load_all(strategy_a, symbol=symbol, timeframe=timeframe)
        entries_b = self.tracker.load_all(strategy_b, symbol=symbol, timeframe=timeframe)

        def build(sid: str, records: list) -> dict[str, Any] | None:
            e = self._build_entry(sid, records)
            if not e:
                return None
            d = e.to_dict()
            d.pop("rank", None)
            return d

        result_a = build(strategy_a, entries_a)
        result_b = build(strategy_b, entries_b)

        if not result_a or not result_b:
            missing = strategy_a if not result_a else strategy_b
            return {"error": f"Nenhum experimento para '{missing}'"}

        # Winner por métrica (maior é melhor, exceto max_drawdown menor é melhor)
        metrics_compare = ["composite_score", "best_sharpe", "best_sortino",
                           "best_calmar", "best_expectancy", "consistency_pct"]

        winners: dict[str, str] = {}
        for m in metrics_compare:
            va = result_a.get(m, 0) or 0
            vb = result_b.get(m, 0) or 0
            winners[m] = strategy_a if va >= vb else strategy_b

        # Drawdown: menor é melhor
        dd_a = result_a.get("max_drawdown", 0) or 0
        dd_b = result_b.get("max_drawdown", 0) or 0
        winners["max_drawdown"] = strategy_a if dd_a <= dd_b else strategy_b

        # Winner geral
        score_a = result_a.get("composite_score", 0) or 0
        score_b = result_b.get("composite_score", 0) or 0
        overall_winner = strategy_a if score_a >= score_b else strategy_b

        # Nível de confiança baseado em # experimentos
        n_a = result_a.get("experiment_count", 0)
        n_b = result_b.get("experiment_count", 0)
        n_min = min(n_a, n_b)
        confidence = "high" if n_min >= 10 else "medium" if n_min >= 3 else "low"

        return {
            "strategy_a":     result_a,
            "strategy_b":     result_b,
            "winners":        winners,
            "overall_winner": overall_winner,
            "confidence":     confidence,
            "note": (
                f"Comparação com {n_a} exp. para '{strategy_a}' e {n_b} exp. para '{strategy_b}'. "
                f"Confiança {confidence} — {'mais experimentos aumentam a significância' if confidence != 'high' else 'amostragem adequada'}."
            ),
        }

    def update_prometheus_scores(
        self,
        symbol:    str | None = None,
        timeframe: str | None = None,
    ) -> None:
        """
        Phase K FASE 7+12: Atualiza o Gauge strategy_composite_score no Prometheus
        com o score atual de cada estratégia.

        Chamado ao final de rank() ou explicitamente via CLI.
        """
        try:
            from api import metrics as prom_metrics
            sids = _discover_strategies()
            for sid in sids:
                records = self.tracker.load_all(sid, symbol=symbol, timeframe=timeframe)
                if not records:
                    continue
                entry = self._build_entry(sid, records)
                if entry:
                    prom_metrics.strategy_composite_score.labels(
                        strategy_id=sid,
                    ).set(entry.composite_score)
        except Exception:  # noqa: BLE001
            pass  # Prometheus não disponível

    # ─── Construção do entry ──────────────────────────────────────────────

    def _build_entry(self, strategy_id: str, records: list) -> StrategyRankEntry | None:
        if not records:
            return None

        # Selecionar o melhor experimento por Sharpe
        def safe_sharpe(r) -> float:
            v = r.metrics.get("sharpe", -999)
            return v if isinstance(v, (int, float)) and not math.isnan(v) else -999

        best = max(records, key=safe_sharpe)

        def m(key: str, default: float = 0.0) -> float:
            v = best.metrics.get(key, default)
            if not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
                return default
            return float(v)

        sharpe     = m("sharpe")
        sortino    = m("sortino")
        calmar     = m("calmar")
        drawdown   = m("max_drawdown", -100.0)
        expectancy = m("expectancy")
        trades     = int(m("total_trades"))

        # Consistência: % de experimentos com sharpe > 0
        positive = sum(1 for r in records if safe_sharpe(r) > 0)
        consistency = positive / len(records)

        # Score composto
        score, breakdown = _compute_score(
            sharpe=sharpe, sortino=sortino, calmar=calmar,
            drawdown=drawdown, expectancy=expectancy, consistency=consistency,
        )

        return StrategyRankEntry(
            strategy_id=strategy_id,
            experiment_count=len(records),
            best_sharpe=sharpe,
            best_sortino=sortino,
            best_calmar=calmar,
            max_drawdown=drawdown,
            best_expectancy=expectancy,
            consistency=consistency,
            total_trades=trades,
            symbol=best.symbol,
            timeframe=best.timeframe,
            run_id=best.run_id,
            composite_score=score,
            score_breakdown=breakdown,
        )


# ── Score composto ────────────────────────────────────────────────────────────

def _normalize(value: float, lo: float, hi: float) -> float:
    """Normaliza value para [0, 1] usando clamp [lo, hi]."""
    if hi == lo:
        return 0.0
    clamped = max(lo, min(hi, value))
    return (clamped - lo) / (hi - lo)


def _compute_score(
    *,
    sharpe:      float,
    sortino:     float,
    calmar:      float,
    drawdown:    float,
    expectancy:  float,
    consistency: float,
) -> tuple[float, dict[str, float]]:
    """
    Retorna (composite_score 0–100, breakdown por componente).
    """
    norm_sharpe     = _normalize(sharpe,     *CLAMP["sharpe"])
    norm_sortino    = _normalize(sortino,    *CLAMP["sortino"])
    norm_calmar     = _normalize(calmar,     *CLAMP["calmar"])
    # drawdown: -80% = 0, 0% = 1
    norm_drawdown   = _normalize(drawdown,   *CLAMP["drawdown"])
    norm_expectancy = _normalize(expectancy, *CLAMP["expectancy"])
    norm_consistency = consistency  # já está em [0, 1]

    breakdown = {
        "sharpe":      norm_sharpe     * WEIGHTS["sharpe"]     * 100,
        "sortino":     norm_sortino    * WEIGHTS["sortino"]    * 100,
        "calmar":      norm_calmar     * WEIGHTS["calmar"]     * 100,
        "drawdown":    norm_drawdown   * WEIGHTS["drawdown"]   * 100,
        "expectancy":  norm_expectancy * WEIGHTS["expectancy"] * 100,
        "consistency": norm_consistency* WEIGHTS["consistency"]* 100,
    }
    total = sum(breakdown.values())
    return round(total, 2), breakdown


# ── Helpers ───────────────────────────────────────────────────────────────────

def _discover_strategies() -> list[str]:
    from .experiment_tracker import EXPERIMENTS_DIR
    if not EXPERIMENTS_DIR.exists():
        return []
    return [
        f.stem
        for f in EXPERIMENTS_DIR.glob("*.jsonl")
        if f.stem != "all_experiments"
    ]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strategy Ranker — ranking composto de estratégias")
    parser.add_argument("--top",           type=int,   default=10,   help="Top N estratégias")
    parser.add_argument("--symbol",        default=None, help="Filtrar por símbolo (ex: BTC/USDT)")
    parser.add_argument("--tf",            default=None, dest="timeframe", help="Filtrar por timeframe")
    parser.add_argument("--min-exp",       type=int,   default=1,    help="Mínimo de experimentos")
    parser.add_argument("--compare",       nargs="+",  default=None, help="Comparar estratégias específicas")
    parser.add_argument("--head-to-head",  nargs=2,   metavar="STRATEGY",
                        help="Comparação formal entre duas estratégias: --head-to-head A B")
    parser.add_argument("--json",          action="store_true", help="Saída em JSON")
    args = parser.parse_args()

    ranker = StrategyRanker()

    if args.head_to_head:
        strategy_a, strategy_b = args.head_to_head
        result = ranker.compare_head_to_head(
            strategy_a, strategy_b,
            symbol=args.symbol,
            timeframe=args.timeframe,
        )
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if "error" in result:
                print(f"Erro: {result['error']}")
            else:
                print(f"\nHead-to-Head: {strategy_a}  vs  {strategy_b}")
                print(f"Overall winner: {result['overall_winner']}  (confiança: {result['confidence']})")
                print(f"\n{result['note']}\n")
                print("Vencedores por métrica:")
                for m, w in result["winners"].items():
                    a_val = result["strategy_a"].get(m, "—")
                    b_val = result["strategy_b"].get(m, "—")
                    marker = "←" if w == strategy_a else "→"
                    print(f"  {m:<22} {marker}  {strategy_a}: {a_val}  |  {strategy_b}: {b_val}")
    else:
        report = ranker.rank(
            symbol=args.symbol,
            timeframe=args.timeframe,
            strategy_ids=args.compare,
            top_n=args.top,
            min_experiments=args.min_exp,
        )

        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.format_table())

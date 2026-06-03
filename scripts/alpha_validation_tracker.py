"""Offline alpha validation tracker for crypto research.

This script is intentionally research-only:
- reads Data-Core PostgreSQL through DATABASE_URL;
- writes local artifacts under runtime-data/research/alpha_validation;
- does not alter production tables, runtime strategy, thresholds, or risk rules.

Discovery hypothesis being validated out-of-sample:
    confidence >= 89.0 and breakout_score >= 65.2625

Those thresholds came from the previous offline research set:
    confidence p80 + breakout_score p75.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from sqlalchemy import create_engine, text


ASSETS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT", "XRP/USDT", "DOGE/USDT")
HORIZONS = (6, 12, 24, 48, 72)

# Frozen discovery thresholds. Do not refit on out-of-sample data.
ALPHA_CONFIDENCE_MIN = 89.0
ALPHA_BREAKOUT_MIN = 65.2625

BASELINE_CONFIDENCE_MIN = 55.0
BASELINE_RSI_MAX = 35.0

DEFAULT_OUTPUT_DIR = Path("runtime-data/research/alpha_validation")
STATE_FILE = "state.json"
OBSERVATIONS_FILE = "observations.csv"
SUMMARY_FILE = "summary.json"
REPORT_FILE = "weekly_alpha_report.md"
MONITOR_THRESHOLDS = (20, 50, 100)
UTC = timezone.utc


@dataclass(frozen=True)
class Metrics:
    n: int
    win_rate: float | None
    expectancy: float | None
    profit_factor: float | None
    avg_mfe: float | None
    avg_mae: float | None
    worst_drawdown: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track crypto alpha candidate out-of-sample.")
    parser.add_argument(
        "command",
        choices=("init", "run", "report"),
        help="init fixes the OOS start timestamp; run refreshes observations; report renders markdown.",
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--start-ts",
        help="ISO timestamp for OOS start. If omitted on init, latest available candle timestamp is used.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=120,
        help="Read-only query window. Only rows after start_ts are evaluated.",
    )
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def dt_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def state_path(output_dir: Path) -> Path:
    return output_dir / STATE_FILE


def load_state(output_dir: Path) -> dict[str, Any]:
    path = state_path(output_dir)
    if not path.exists():
        raise SystemExit(f"State not initialized: run `python scripts/alpha_validation_tracker.py init` first.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(output_dir: Path, state: dict[str, Any]) -> None:
    ensure_output_dir(output_dir)
    state_path(output_dir).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def previous_alpha_count(output_dir: Path) -> int:
    summary_path = output_dir / SUMMARY_FILE
    if not summary_path.exists():
        return 0
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    return int(summary.get("groups", {}).get("alpha_candidate", {}).get("signal_count") or 0)


def connect(database_url: str | None):
    if not database_url:
        raise SystemExit("DATABASE_URL is required via environment or --database-url.")
    return create_engine(database_url)


def latest_candle_timestamp(engine) -> datetime:
    query = text(
        """
        select max(timestamp) as max_ts
        from normalized_market_candles
        where symbol = any(:assets)
        """
    )
    with engine.connect() as conn:
        value = conn.execute(query, {"assets": list(ASSETS)}).scalar_one()
    if value is None:
        raise SystemExit("No normalized_market_candles found for tracked assets.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def init_state(args: argparse.Namespace) -> None:
    engine = connect(args.database_url)
    start_ts = parse_dt(args.start_ts) if args.start_ts else latest_candle_timestamp(engine)
    state = {
        "created_at": dt_iso(utc_now()),
        "start_ts": dt_iso(start_ts),
        "hypothesis": "confidence>=89.0 AND breakout_score>=65.2625",
        "baseline": "RSI<35 AND confidence>=55 AND research entry proxy",
        "thresholds": {
            "alpha_confidence_min": ALPHA_CONFIDENCE_MIN,
            "alpha_breakout_min": ALPHA_BREAKOUT_MIN,
            "baseline_confidence_min": BASELINE_CONFIDENCE_MIN,
            "baseline_rsi_max": BASELINE_RSI_MAX,
        },
    }
    save_state(args.output_dir, state)
    print(json.dumps(state, indent=2))


def fetch_rows(engine, lookback_days: int) -> list[dict[str, Any]]:
    since = utc_now() - timedelta(days=lookback_days)
    query = text(
        """
        select
            ta.id as analytics_id,
            ta.calculated_at,
            n.symbol,
            n.timeframe,
            n.timestamp as candle_timestamp,
            n.open,
            n.high,
            n.low,
            n.close,
            ta.confidence,
            ta.regime,
            ta.rsi,
            ta.moving_average_fast as ma_fast,
            ta.moving_average_slow as ma_slow,
            ta.breakout_score,
            ta.atr,
            ta.adx,
            ta.volume_ratio,
            ta.trend_score
        from normalized_market_candles n
        join trading_analytics ta on ta.market_candle_id = n.id
        where ta.calculated_at >= :since
          and n.symbol = any(:assets)
        order by n.symbol, n.timeframe, n.timestamp, ta.calculated_at, ta.id
        """
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(query, {"since": since, "assets": list(ASSETS)}).mappings()]


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_observations(rows: list[dict[str, Any]], start_ts: datetime) -> list[dict[str, Any]]:
    by_series: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["symbol"], row["timeframe"])
        by_series.setdefault(key, []).append(row)

    observations: list[dict[str, Any]] = []
    seen_analytics_ids: set[str] = set()
    for (symbol, timeframe), series in by_series.items():
        series.sort(key=lambda item: (item["candle_timestamp"], item["calculated_at"], str(item["analytics_id"])))
        for idx, row in enumerate(series):
            analytics_id = str(row["analytics_id"])
            if analytics_id in seen_analytics_ids:
                continue
            seen_analytics_ids.add(analytics_id)

            calculated_at = row["calculated_at"]
            if calculated_at.tzinfo is None:
                calculated_at = calculated_at.replace(tzinfo=UTC)
            calculated_at = calculated_at.astimezone(UTC)
            if calculated_at <= start_ts:
                continue

            candle_ts = row["candle_timestamp"]
            if candle_ts.tzinfo is None:
                candle_ts = candle_ts.replace(tzinfo=UTC)
            candle_ts = candle_ts.astimezone(UTC)

            confidence = as_float(row.get("confidence"))
            breakout = as_float(row.get("breakout_score"))
            rsi = as_float(row.get("rsi"))
            close = as_float(row.get("close"))
            if confidence is None or breakout is None or rsi is None or close is None:
                continue

            baseline_signal = rsi < BASELINE_RSI_MAX and confidence >= BASELINE_CONFIDENCE_MIN
            alpha_signal = baseline_signal and confidence >= ALPHA_CONFIDENCE_MIN and breakout >= ALPHA_BREAKOUT_MIN
            if not baseline_signal and not alpha_signal:
                continue

            obs: dict[str, Any] = {
                "analytics_id": analytics_id,
                "timestamp": dt_iso(calculated_at),
                "calculated_at": dt_iso(calculated_at),
                "candle_timestamp": dt_iso(candle_ts),
                "symbol": symbol,
                "timeframe": timeframe,
                "group_baseline": int(baseline_signal),
                "group_alpha_candidate": int(alpha_signal),
                "regime": row.get("regime"),
                "confidence": confidence,
                "breakout_score": breakout,
                "rsi": rsi,
                "atr": as_float(row.get("atr")),
                "adx": as_float(row.get("adx")),
                "volume_ratio": as_float(row.get("volume_ratio")),
                "trend_score": as_float(row.get("trend_score")),
            }
            for horizon in HORIZONS:
                future = series[idx + 1 : idx + 1 + horizon]
                if len(future) < horizon:
                    obs[f"outcome_{horizon}h"] = None
                    obs[f"mfe_{horizon}h"] = None
                    obs[f"mae_{horizon}h"] = None
                    continue
                future_close = as_float(future[-1].get("close"))
                highs = [as_float(item.get("high")) for item in future]
                lows = [as_float(item.get("low")) for item in future]
                highs_f = [value for value in highs if value is not None]
                lows_f = [value for value in lows if value is not None]
                obs[f"outcome_{horizon}h"] = pct_return(future_close, close) if future_close is not None else None
                obs[f"mfe_{horizon}h"] = pct_return(max(highs_f), close) if highs_f else None
                obs[f"mae_{horizon}h"] = pct_return(min(lows_f), close) if lows_f else None
            observations.append(obs)
    return observations


def pct_return(exit_price: float, entry_price: float) -> float:
    return ((exit_price - entry_price) / entry_price) * 100.0


def write_observations(output_dir: Path, observations: list[dict[str, Any]]) -> None:
    ensure_output_dir(output_dir)
    path = output_dir / OBSERVATIONS_FILE
    fieldnames = [
        "analytics_id",
        "timestamp",
        "calculated_at",
        "candle_timestamp",
        "symbol",
        "timeframe",
        "group_baseline",
        "group_alpha_candidate",
        "regime",
        "confidence",
        "breakout_score",
        "rsi",
        "atr",
        "adx",
        "volume_ratio",
        "trend_score",
    ]
    for horizon in HORIZONS:
        fieldnames.extend([f"outcome_{horizon}h", f"mfe_{horizon}h", f"mae_{horizon}h"])
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(observations)


def metric(rows: list[dict[str, Any]], horizon: int) -> Metrics:
    outcomes = [as_float(row.get(f"outcome_{horizon}h")) for row in rows]
    values = [value for value in outcomes if value is not None]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    mfes = [as_float(row.get(f"mfe_{horizon}h")) for row in rows]
    maes = [as_float(row.get(f"mae_{horizon}h")) for row in rows]
    mfes_f = [value for value in mfes if value is not None]
    maes_f = [value for value in maes if value is not None]
    if not values:
        return Metrics(0, None, None, None, None, None, None)
    profit_factor = None
    if losses:
        profit_factor = sum(wins) / abs(sum(losses)) if wins else 0.0
    return Metrics(
        n=len(values),
        win_rate=(len(wins) / len(values)) * 100.0,
        expectancy=mean(values),
        profit_factor=profit_factor,
        avg_mfe=mean(mfes_f) if mfes_f else None,
        avg_mae=mean(maes_f) if maes_f else None,
        worst_drawdown=min(maes_f) if maes_f else None,
    )


def metrics_by_group(observations: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {
        "baseline": [row for row in observations if row["group_baseline"]],
        "alpha_candidate": [row for row in observations if row["group_alpha_candidate"]],
    }
    summary: dict[str, Any] = {}
    for group_name, rows in groups.items():
        summary[group_name] = {
            "signal_count": len(rows),
            "horizons": {f"{h}h": metric_to_dict(metric(rows, h)) for h in HORIZONS},
            "regimes": {},
        }
        for regime in sorted({str(row.get("regime")) for row in rows}):
            regime_rows = [row for row in rows if str(row.get("regime")) == regime]
            summary[group_name]["regimes"][regime] = {
                "signal_count": len(regime_rows),
                "horizons": {f"{h}h": metric_to_dict(metric(regime_rows, h)) for h in HORIZONS},
            }
    return summary


def metric_to_dict(value: Metrics) -> dict[str, Any]:
    return {
        "outcome_count": value.n,
        "win_rate": round(value.win_rate, 4) if value.win_rate is not None else None,
        "expectancy": round(value.expectancy, 6) if value.expectancy is not None else None,
        "profit_factor": round(value.profit_factor, 6) if value.profit_factor is not None else None,
        "avg_mfe": round(value.avg_mfe, 6) if value.avg_mfe is not None else None,
        "avg_mae": round(value.avg_mae, 6) if value.avg_mae is not None else None,
        "worst_drawdown": round(value.worst_drawdown, 6) if value.worst_drawdown is not None else None,
    }


def run(args: argparse.Namespace) -> None:
    state = load_state(args.output_dir)
    start_ts = parse_dt(state["start_ts"])
    previous_count = int(state.get("last_alpha_signal_count", previous_alpha_count(args.output_dir)) or 0)
    engine = connect(args.database_url)
    observations = compute_observations(fetch_rows(engine, args.lookback_days), start_ts)
    write_observations(args.output_dir, observations)
    groups = metrics_by_group(observations)
    monitor = monitor_status(start_ts, groups["alpha_candidate"]["signal_count"], previous_count)
    summary = {
        "generated_at": dt_iso(utc_now()),
        "start_ts": state["start_ts"],
        "thresholds": state["thresholds"],
        "success_criteria": {
            "min_outcomes": 100,
            "min_profit_factor": 1.5,
            "positive_expectancy": True,
            "out_of_sample_only": True,
        },
        "monitor": monitor,
        "groups": groups,
        "verdict": verdict(groups),
    }
    (args.output_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    state["last_alpha_signal_count"] = groups["alpha_candidate"]["signal_count"]
    state["last_run_at"] = summary["generated_at"]
    save_state(args.output_dir, state)
    render_report(args.output_dir, summary)
    print(json.dumps(summary, indent=2))


def monitor_status(start_ts: datetime, current_n: int, previous_n: int) -> dict[str, Any]:
    elapsed_days = max((utc_now() - start_ts).total_seconds() / 86400.0, 0.0)
    candidates_per_day = current_n / elapsed_days if elapsed_days > 0 else None
    return {
        "status": sample_status(current_n),
        "current_n": current_n,
        "previous_n": previous_n,
        "days_since_start": round(elapsed_days, 4),
        "candidates_per_day": round(candidates_per_day, 6) if candidates_per_day is not None else None,
        "projections": {
            f"n_{target}": projection(current_n, target, candidates_per_day) for target in MONITOR_THRESHOLDS
        },
        "alerts": alpha_alerts(previous_n, current_n),
    }


def sample_status(current_n: int) -> str:
    if current_n == 0:
        return "WAITING_FOR_FIRST_SAMPLE"
    if current_n < 20:
        return "BOOTSTRAPPING"
    if current_n < 50:
        return "EARLY_SIGNAL"
    if current_n < 100:
        return "PRELIMINARY"
    return "VALIDATION"


def projection(current_n: int, target: int, candidates_per_day: float | None) -> dict[str, Any]:
    remaining = max(target - current_n, 0)
    if remaining == 0:
        return {"remaining": 0, "eta_days": 0.0}
    if not candidates_per_day or candidates_per_day <= 0:
        return {"remaining": remaining, "eta_days": None}
    return {"remaining": remaining, "eta_days": round(remaining / candidates_per_day, 2)}


def alpha_alerts(previous_n: int, current_n: int) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if previous_n == 0 and current_n >= 1:
        alerts.append(
            {
                "name": "First Alpha Candidate Observed",
                "reason": f"alpha candidate count moved from {previous_n} to {current_n}",
            }
        )
    if previous_n > 0 and current_n >= previous_n * 1.25:
        alerts.append(
            {
                "name": "Alpha Dataset Growth",
                "reason": f"alpha candidate count grew from {previous_n} to {current_n}",
            }
        )
    if previous_n < 100 <= current_n:
        alerts.append(
            {
                "name": "Alpha Validation Ready",
                "reason": f"alpha candidate count reached {current_n}",
            }
        )
    return alerts


def verdict(summary: dict[str, Any]) -> str:
    alpha = summary.get("alpha_candidate", {})
    h24 = alpha.get("horizons", {}).get("24h", {})
    if (h24.get("outcome_count") or 0) >= 100 and (h24.get("profit_factor") or 0) > 1.5 and (
        h24.get("expectancy") or 0
    ) > 0:
        return "REPRODUCIBLE_ALPHA"
    if (h24.get("outcome_count") or 0) > 0:
        return "PROMISING"
    return "HISTORICAL_ONLY"


def render_report(output_dir: Path, summary: dict[str, Any] | None = None) -> None:
    if summary is None:
        summary_path = output_dir / SUMMARY_FILE
        if not summary_path.exists():
            raise SystemExit("No summary found. Run `run` first.")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    lines = [
        "# Weekly Alpha Validation Report",
        "",
        f"Generated at: {summary['generated_at']}",
        f"Out-of-sample start: {summary['start_ts']}",
        f"Verdict: {summary['verdict']}",
        "",
        "## Monitor Status",
        "",
        "| Status | Current N | Previous N | Days Since Start | Candidates/Day | ETA N=20 | ETA N=50 | ETA N=100 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        monitor_row(summary["monitor"]),
        "",
        "## Alerts",
        "",
    ]
    alerts = summary["monitor"].get("alerts") or []
    if alerts:
        lines.extend(["| Alert | Reason |", "|---|---|"])
        for alert in alerts:
            lines.append(f"| {alert['name']} | {alert['reason']} |")
    else:
        lines.append("No alpha monitor alerts triggered in this run.")
    lines.extend(
        [
            "",
            "## Baseline vs Alpha Candidate",
            "",
            "| Group | Signals | Horizon | Outcomes | WR | PF | Expectancy | Avg MFE | Avg MAE | Worst DD |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group_name, group in summary["groups"].items():
        for horizon, h in group["horizons"].items():
            lines.append(
                "| {group} | {signals} | {horizon} | {outcomes} | {wr} | {pf} | {exp} | {mfe} | {mae} | {dd} |".format(
                    group=group_name,
                    signals=group["signal_count"],
                    horizon=horizon,
                    outcomes=h["outcome_count"],
                    wr=fmt(h["win_rate"]),
                    pf=fmt(h["profit_factor"]),
                    exp=fmt(h["expectancy"]),
                    mfe=fmt(h["avg_mfe"]),
                    mae=fmt(h["avg_mae"]),
                    dd=fmt(h["worst_drawdown"]),
                )
            )
    lines.extend(["", "## Regime Validation", ""])
    lines.extend(
        [
            "| Group | Regime | Signals | Horizon | Outcomes | WR | PF | Expectancy |",
            "|---|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    for group_name, group in summary["groups"].items():
        for regime, regime_summary in group["regimes"].items():
            for horizon, h in regime_summary["horizons"].items():
                lines.append(
                    "| {group} | {regime} | {signals} | {horizon} | {outcomes} | {wr} | {pf} | {exp} |".format(
                        group=group_name,
                        regime=regime,
                        signals=regime_summary["signal_count"],
                        horizon=horizon,
                        outcomes=h["outcome_count"],
                        wr=fmt(h["win_rate"]),
                        pf=fmt(h["profit_factor"]),
                        exp=fmt(h["expectancy"]),
                    )
                )
    lines.append("")
    (output_dir / REPORT_FILE).write_text("\n".join(lines), encoding="utf-8")


def monitor_row(monitor: dict[str, Any]) -> str:
    projections = monitor.get("projections", {})
    return (
        "| {status} | {current} | {previous} | {days} | {rate} | {eta20} | {eta50} | {eta100} |".format(
            status=monitor.get("status"),
            current=monitor.get("current_n"),
            previous=monitor.get("previous_n"),
            days=fmt(monitor.get("days_since_start")),
            rate=fmt(monitor.get("candidates_per_day")),
            eta20=fmt(projections.get("n_20", {}).get("eta_days")),
            eta50=fmt(projections.get("n_50", {}).get("eta_days")),
            eta100=fmt(projections.get("n_100", {}).get("eta_days")),
        )
    )


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def report(args: argparse.Namespace) -> None:
    render_report(args.output_dir)
    print((args.output_dir / REPORT_FILE).read_text(encoding="utf-8"))


def main() -> None:
    args = parse_args()
    if args.command == "init":
        init_state(args)
    elif args.command == "run":
        run(args)
    elif args.command == "report":
        report(args)


if __name__ == "__main__":
    main()

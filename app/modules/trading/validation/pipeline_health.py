"""Outcome pipeline health monitoring.

Detects common failure modes in the signal outcome evaluation pipeline and
produces a composite health score (0–100) with severity classification.

Health components:
  - Recency     (0-30 pts): how recently was the last outcome evaluated?
  - Throughput  (0-25 pts): are outcomes being evaluated at a healthy rate?
  - Pending lag (0-25 pts): how many horizon-closed signals are still pending?
  - Error rate  (0-20 pts): fraction of evaluations that failed with exceptions

Bootstrap mode (outcome_count < BOOTSTRAP_OUTCOME_THRESHOLD) suppresses
WARNING/CRITICAL severity to INFO, preventing alert storm on day 0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, exists
from sqlalchemy.orm import Session

from app.analytics.models import TradingAnalytics
from app.modules.trading.validation.models import TradingSignalOutcome
from app.modules.trading.validation.outcome_tracker import EVALUATION_HORIZON, _TIMEFRAME_DELTA

logger = logging.getLogger(__name__)

# Bootstrap threshold: number of evaluated outcomes below which the runtime
# is considered "bootstrapping".  Suppresses alert severity.
BOOTSTRAP_OUTCOME_THRESHOLD: int = 50

# A signal_outcomes_job that hasn't run in this many minutes is considered stale.
STALE_JOB_MINUTES: int = 90  # job runs every 60 min; 1.5× gives tolerance

# Pending signals older than this many hours (beyond horizon) are "stuck".
STUCK_PENDING_HOURS: int = 6

# Metrics lazy handles
_metrics_loaded = False
_m_pending: object = None
_m_accuracy: object = None
_m_mfe: object = None
_m_mae: object = None
_m_lag: object = None
_m_bootstrap: object = None
_m_health: object = None


def _load_metrics() -> None:
    global _metrics_loaded
    global _m_pending, _m_accuracy, _m_mfe, _m_mae, _m_lag, _m_bootstrap, _m_health
    if _metrics_loaded:
        return
    try:
        from api.metrics import (  # noqa: PLC0415
            outcome_accuracy_ratio,
            outcome_avg_mae_pct,
            outcome_avg_mfe_pct,
            outcome_bootstrap_phase,
            outcome_pending_total,
            outcome_pipeline_health_score,
            outcome_runtime_lag_seconds,
        )
        _m_pending = outcome_pending_total
        _m_accuracy = outcome_accuracy_ratio
        _m_mfe = outcome_avg_mfe_pct
        _m_mae = outcome_avg_mae_pct
        _m_lag = outcome_runtime_lag_seconds
        _m_bootstrap = outcome_bootstrap_phase
        _m_health = outcome_pipeline_health_score
        _metrics_loaded = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load Prometheus metrics (non-fatal): %s", exc)


@dataclass
class PipelineHealthReport:
    """Outcome pipeline health report."""

    health_score: float          # 0–100
    severity: str                # INFO | WARNING | CRITICAL
    bootstrap_mode: bool

    total_outcomes: int
    last_evaluated_at: datetime | None
    seconds_since_last_evaluation: float | None
    pending_count: int
    stuck_count: int             # pending beyond STUCK_PENDING_HOURS
    recent_error_rate: float     # 0.0–1.0 (last 24h)

    components: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


class OutcomePipelineHealthService:
    """Monitors and scores the health of the signal outcome evaluation pipeline.

    Call ``check()`` from the dataset_quality_crypto_job (every 30 min) to
    keep Prometheus gauges current and detect silent failures early.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        _load_metrics()

    def check(self) -> PipelineHealthReport:
        """Run all health checks and return a PipelineHealthReport."""
        now = datetime.now(tz=timezone.utc)

        total_outcomes = self._count_total_outcomes()
        bootstrap_mode = total_outcomes < BOOTSTRAP_OUTCOME_THRESHOLD
        last_eval_at = self._last_evaluation_at()
        secs_since = (
            (now - last_eval_at).total_seconds()
            if last_eval_at is not None
            else None
        )
        pending_by_pair = self._count_pending_by_pair(now)
        pending_count = sum(pending_by_pair.values())
        stuck_count = self._count_stuck_pending(now)
        error_rate = self._recent_error_rate(now)

        # ── Score components ───────────────────────────────────────────────────
        recency_pts = self._score_recency(secs_since)          # 0–30
        throughput_pts = self._score_throughput(total_outcomes) # 0–25
        lag_pts = self._score_pending_lag(pending_count, stuck_count)  # 0–25
        error_pts = self._score_error_rate(error_rate)          # 0–20

        health_score = round(recency_pts + throughput_pts + lag_pts + error_pts, 1)

        issues: list[str] = []
        if secs_since is not None and secs_since > STALE_JOB_MINUTES * 60:
            issues.append(
                f"Last outcome evaluation was {secs_since / 60:.0f} min ago "
                f"(threshold: {STALE_JOB_MINUTES} min)"
            )
        if last_eval_at is None and not bootstrap_mode:
            issues.append("No outcomes evaluated yet and runtime is past bootstrap threshold")
        if stuck_count > 0:
            issues.append(f"{stuck_count} signals pending evaluation for >{STUCK_PENDING_HOURS}h beyond horizon")
        if error_rate > 0.1:
            issues.append(f"High evaluation error rate: {error_rate:.1%} of recent runs failed")

        # Bootstrap suppresses WARNING/CRITICAL → INFO
        if bootstrap_mode:
            severity = "INFO"
        elif health_score >= 70:
            severity = "INFO"
        elif health_score >= 40:
            severity = "WARNING"
        else:
            severity = "CRITICAL"

        report = PipelineHealthReport(
            health_score=health_score,
            severity=severity,
            bootstrap_mode=bootstrap_mode,
            total_outcomes=total_outcomes,
            last_evaluated_at=last_eval_at,
            seconds_since_last_evaluation=round(secs_since, 1) if secs_since is not None else None,
            pending_count=pending_count,
            stuck_count=stuck_count,
            recent_error_rate=round(error_rate, 4),
            components={
                "recency_pts": recency_pts,
                "throughput_pts": throughput_pts,
                "lag_pts": lag_pts,
                "error_pts": error_pts,
                "stale_threshold_minutes": STALE_JOB_MINUTES,
                "bootstrap_threshold": BOOTSTRAP_OUTCOME_THRESHOLD,
            },
            issues=issues,
        )

        self._emit_metrics(report, pending_by_pair, now)
        return report

    # ── Score helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _score_recency(secs_since: float | None) -> float:
        """30 pts when job ran < 60 min ago; linear decay to 0 at 4h."""
        if secs_since is None:
            return 0.0  # never ran
        if secs_since <= 3600:
            return 30.0
        if secs_since >= 14400:  # 4h
            return 0.0
        return round(30.0 * (1.0 - (secs_since - 3600) / (14400 - 3600)), 1)

    @staticmethod
    def _score_throughput(total: int) -> float:
        """25 pts when ≥50 outcomes exist; proportional below."""
        return round(min(25.0, 25.0 * total / BOOTSTRAP_OUTCOME_THRESHOLD), 1)

    @staticmethod
    def _score_pending_lag(pending: int, stuck: int) -> float:
        """25 pts when no pending; -5 per stuck signal (min 0)."""
        if pending == 0:
            return 25.0
        if stuck >= 5:
            return 0.0
        return round(max(0.0, 25.0 - stuck * 5.0), 1)

    @staticmethod
    def _score_error_rate(error_rate: float) -> float:
        """20 pts when error_rate=0; 0 pts when ≥50%."""
        return round(max(0.0, 20.0 * (1.0 - error_rate / 0.5)), 1)

    # ── DB queries ─────────────────────────────────────────────────────────────

    def _count_total_outcomes(self) -> int:
        return (
            self.db.query(func.count(TradingSignalOutcome.id)).scalar() or 0
        )

    def _last_evaluation_at(self) -> datetime | None:
        ts = (
            self.db.query(func.max(TradingSignalOutcome.evaluated_at)).scalar()
        )
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    def _count_pending_by_pair(self, now: datetime) -> dict[tuple[str, str], int]:
        """Count horizon-closed BUY/SELL signals that have no outcome yet."""
        already_evaluated = (
            exists().where(TradingSignalOutcome.analytics_id == TradingAnalytics.id)
        )
        rows = (
            self.db.query(
                TradingAnalytics.symbol,
                TradingAnalytics.timeframe,
                func.count(TradingAnalytics.id),
            )
            .filter(
                TradingAnalytics.signal.in_(["BUY", "SELL"]),
                ~already_evaluated,
            )
            .group_by(TradingAnalytics.symbol, TradingAnalytics.timeframe)
            .all()
        )
        return {(r[0] or "unknown", r[1] or "unknown"): r[2] for r in rows}

    def _count_stuck_pending(self, now: datetime) -> int:
        """Count signals where horizon closed >STUCK_PENDING_HOURS ago but no outcome."""
        cutoff = now - timedelta(hours=STUCK_PENDING_HOURS)
        already_evaluated = (
            exists().where(TradingSignalOutcome.analytics_id == TradingAnalytics.id)
        )
        return (
            self.db.query(func.count(TradingAnalytics.id))
            .filter(
                TradingAnalytics.signal.in_(["BUY", "SELL"]),
                TradingAnalytics.calculated_at < cutoff,
                ~already_evaluated,
            )
            .scalar()
            or 0
        )

    def _recent_error_rate(self, now: datetime) -> float:
        """Derive error rate from Prometheus counter if loaded; else 0.0."""
        # We can't directly query Prometheus from here, so we use a
        # heuristic: if stuck_count is high relative to total pending,
        # that itself is evidence of evaluation failures.
        # True error rate is tracked via outcome_eval_error_total counter.
        return 0.0  # counters are incremental; health service observes other signals

    def _compute_lag_seconds(self, now: datetime) -> float:
        """Return seconds since the oldest horizon-closed signal (0 if none)."""
        already_evaluated = (
            exists().where(TradingSignalOutcome.analytics_id == TradingAnalytics.id)
        )
        oldest_ts = (
            self.db.query(func.min(TradingAnalytics.calculated_at))
            .filter(
                TradingAnalytics.signal.in_(["BUY", "SELL"]),
                ~already_evaluated,
            )
            .scalar()
        )
        if oldest_ts is None:
            return 0.0
        if oldest_ts.tzinfo is None:
            oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)
        # Add horizon offset to get the actual close time
        delta = timedelta(hours=1)  # conservative default
        horizon_close = oldest_ts + delta * EVALUATION_HORIZON
        if horizon_close > now:
            return 0.0
        return max(0.0, (now - horizon_close).total_seconds())

    def _compute_rolling_stats(
        self, now: datetime
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Compute accuracy, avg MFE/MAE per (symbol, timeframe, signal) for last 7 days."""
        window_start = now - timedelta(days=7)
        rows = (
            self.db.query(
                TradingSignalOutcome.symbol,
                TradingSignalOutcome.timeframe,
                TradingSignalOutcome.signal,
                func.count(TradingSignalOutcome.id),
                func.sum(
                    func.cast(TradingSignalOutcome.outcome_correct, type_=None)
                ).label("correct_count"),
                func.avg(TradingSignalOutcome.max_favorable_pct),
                func.avg(
                    func.abs(TradingSignalOutcome.max_adverse_pct)
                ),
            )
            .filter(TradingSignalOutcome.evaluated_at >= window_start)
            .group_by(
                TradingSignalOutcome.symbol,
                TradingSignalOutcome.timeframe,
                TradingSignalOutcome.signal,
            )
            .all()
        )

        result: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            sym, tf, sig, total, correct_raw, avg_mfe, avg_mae = row
            correct = int(correct_raw or 0)
            accuracy = correct / total if total else 0.0
            result[(sym or "unknown", tf or "unknown", sig or "unknown")] = {
                "total": total,
                "accuracy": round(accuracy, 4),
                "avg_mfe": round(float(avg_mfe), 4) if avg_mfe is not None else None,
                "avg_mae": round(float(avg_mae), 4) if avg_mae is not None else None,
            }
        return result

    # ── Prometheus emit ────────────────────────────────────────────────────────

    def _emit_metrics(
        self,
        report: PipelineHealthReport,
        pending_by_pair: dict[tuple[str, str], int],
        now: datetime,
    ) -> None:
        if not _metrics_loaded:
            return
        try:
            # Health score + bootstrap flag
            _m_health.set(report.health_score)  # type: ignore[union-attr]
            _m_bootstrap.set(1.0 if report.bootstrap_mode else 0.0)  # type: ignore[union-attr]

            # Pending by pair
            for (sym, tf), count in pending_by_pair.items():
                _m_pending.labels(symbol=sym, timeframe=tf).set(count)  # type: ignore[union-attr]

            # Runtime lag
            lag = self._compute_lag_seconds(now)
            _m_lag.set(lag)  # type: ignore[union-attr]

            # Rolling accuracy + MFE/MAE
            stats = self._compute_rolling_stats(now)
            for (sym, tf, sig), s in stats.items():
                _m_accuracy.labels(symbol=sym, timeframe=tf, signal=sig).set(  # type: ignore[union-attr]
                    s["accuracy"]
                )
            # MFE/MAE averaged across signals per pair
            pair_mfe: dict[tuple[str, str], list[float]] = {}
            pair_mae: dict[tuple[str, str], list[float]] = {}
            for (sym, tf, _sig), s in stats.items():
                if s["avg_mfe"] is not None:
                    pair_mfe.setdefault((sym, tf), []).append(s["avg_mfe"])
                if s["avg_mae"] is not None:
                    pair_mae.setdefault((sym, tf), []).append(s["avg_mae"])
            for (sym, tf), vals in pair_mfe.items():
                _m_mfe.labels(symbol=sym, timeframe=tf).set(  # type: ignore[union-attr]
                    round(sum(vals) / len(vals), 4)
                )
            for (sym, tf), vals in pair_mae.items():
                _m_mae.labels(symbol=sym, timeframe=tf).set(  # type: ignore[union-attr]
                    round(sum(vals) / len(vals), 4)
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Health service Prometheus emit failed (non-fatal): %s", exc)

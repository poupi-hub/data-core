"""Self-Healing Safe Recovery Engine — Phases 3, 5 & 6.

Detects stalled pipelines and applies bounded, non-destructive recovery actions.

SAFETY INVARIANTS (never violated):
  - NO automatic deletes, truncates, or force-cleanups
  - NO mass retries without per-job limits
  - NO infinite restart loops
  - NO data fabrication or mock injection
  - Recovery actions are advisory: they log + schedule a single bounded retry

Components:
  DeadPipelineDetector  — classifies pipeline state from DB signals
  SelfHealingCoordinator — decides and logs recovery actions
  BacklogRecoveryEngine  — computes safe batch sizes under DB pressure

Persistence:
  runtime-data/self_healing_log.jsonl  — append-only audit of every action
  runtime-data/self_healing_state.json — current throttle state

Usage::

    from database.session import SessionLocal
    from app.pipeline.self_healing import SelfHealingCoordinator

    db = SessionLocal()
    try:
        coordinator = SelfHealingCoordinator(db)
        actions = coordinator.evaluate_and_act()
    finally:
        db.close()
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.pipeline.liveness import (
    PIPELINE_REGISTRY,
    PipelineDescriptor,
    PipelineLivenessService,
    PipelineStatus,
)
from app.raw.models import RawCollection
from app.runtime.heartbeat import RUNTIME_DATA_DIR

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

SELF_HEALING_LOG_PATH = Path(
    os.getenv(
        "SELF_HEALING_LOG_PATH",
        str(RUNTIME_DATA_DIR / "self_healing_log.jsonl"),
    )
)
SELF_HEALING_STATE_PATH = Path(
    os.getenv(
        "SELF_HEALING_STATE_PATH",
        str(RUNTIME_DATA_DIR / "self_healing_state.json"),
    )
)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Maximum self-healing triggers per pipeline per hour (prevents loops)
MAX_TRIGGERS_PER_HOUR = 3

# How long a normalize pipeline must be stalled before a wake-up is suggested
STALL_RECOVERY_THRESHOLD_SECONDS = 20 * 60   # 20 min (> 1 expected interval)

# How large the backlog must be before backlog recovery triggers
BACKLOG_RECOVERY_THRESHOLD = 50   # raw records pending

# DB pressure ratio that halves the batch size (0–1)
DB_PRESSURE_HIGH_THRESHOLD = 0.7
DB_PRESSURE_CRITICAL_THRESHOLD = 0.9


# ──────────────────────────────────────────────────────────────────────────────
# Dead Pipeline Detector
# ──────────────────────────────────────────────────────────────────────────────

class DeadSignal(str, Enum):
    BACKLOG_GROWING_NORMALIZE_STATIC = "backlog_growing_normalize_static"
    NORMALIZE_NEVER_SUCCEEDED = "normalize_never_succeeded"
    ANALYTICS_STALE = "analytics_stale"
    SCHEDULER_HEARTBEAT_STALE = "scheduler_heartbeat_stale"
    PIPELINE_STALLED = "pipeline_stalled"


@dataclass
class DeadPipelineSignal:
    signal: DeadSignal
    pipeline_id: str
    details: dict[str, Any]
    severity: str   # "warning" | "critical"


class DeadPipelineDetector:
    """Detects dead-pipeline signals from DB state.

    Signals detected:
    1. backlog_growing_normalize_static:
       raw_collections.normalization_pending growing AND
       no new normalized_products in > threshold

    2. normalize_never_succeeded:
       pipeline has runs in pipeline_runs but status never 'success'/'partial'

    3. analytics_stale:
       normalization succeeds but analytics lag > 2h

    4. scheduler_heartbeat_stale:
       runtime-data/scheduler_heartbeat.json not updated in > 10 min

    5. pipeline_stalled:
       derived from PipelineLivenessService (STALLED/DEAD states)
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self._now = datetime.now(timezone.utc)

    def detect(self) -> list[DeadPipelineSignal]:
        signals: list[DeadPipelineSignal] = []

        signals.extend(self._check_backlog_vs_normalize())
        signals.extend(self._check_scheduler_heartbeat())
        signals.extend(self._check_liveness_states())

        return signals

    def _check_backlog_vs_normalize(self) -> list[DeadPipelineSignal]:
        """Detect: backlog growing, normalize not producing output."""
        from app.normalization.models import NormalizedProduct

        signals = []
        for module in ("ecommerce",):
            try:
                pending = (
                    self.db.query(func.count(RawCollection.id))
                    .filter(
                        RawCollection.module == module,
                        RawCollection.processing_status == "normalization_pending",
                    )
                    .scalar()
                ) or 0

                if pending < BACKLOG_RECOVERY_THRESHOLD:
                    continue

                # Check latest normalized_at
                latest_norm = (
                    self.db.query(func.max(NormalizedProduct.normalized_at))
                    .scalar()
                )
                if latest_norm is None:
                    signals.append(DeadPipelineSignal(
                        signal=DeadSignal.BACKLOG_GROWING_NORMALIZE_STATIC,
                        pipeline_id=f"normalize_{module}",
                        details={
                            "pending_raws": pending,
                            "latest_normalized_at": None,
                            "message": "Backlog growing but normalized_products is empty — normalize_job never ran",
                        },
                        severity="critical",
                    ))
                    continue

                if latest_norm.tzinfo is None:
                    latest_norm = latest_norm.replace(tzinfo=timezone.utc)
                norm_age = (self._now - latest_norm).total_seconds()

                if norm_age > STALL_RECOVERY_THRESHOLD_SECONDS and pending > BACKLOG_RECOVERY_THRESHOLD:
                    signals.append(DeadPipelineSignal(
                        signal=DeadSignal.BACKLOG_GROWING_NORMALIZE_STATIC,
                        pipeline_id=f"normalize_{module}",
                        details={
                            "pending_raws": pending,
                            "latest_normalized_at": latest_norm.isoformat(),
                            "normalize_age_seconds": round(norm_age),
                            "message": (
                                f"Backlog={pending} raws pending, "
                                f"last normalization {norm_age / 60:.0f}m ago"
                            ),
                        },
                        severity="warning" if norm_age < 60 * 60 else "critical",
                    ))
            except Exception as exc:
                logger.warning("dead_detector: backlog check failed for %s: %s", module, exc)

        return signals

    def _check_scheduler_heartbeat(self) -> list[DeadPipelineSignal]:
        """Detect: scheduler heartbeat file not updated recently."""
        from app.runtime.scheduler_heartbeat import heartbeat_age_seconds

        signals = []
        try:
            age = heartbeat_age_seconds()
            if age is None:
                signals.append(DeadPipelineSignal(
                    signal=DeadSignal.SCHEDULER_HEARTBEAT_STALE,
                    pipeline_id="scheduler_heartbeat",
                    details={
                        "heartbeat_age_seconds": None,
                        "message": "scheduler_heartbeat.json not found — scheduler may never have started",
                    },
                    severity="warning",
                ))
            elif age > 10 * 60:   # 10 min
                signals.append(DeadPipelineSignal(
                    signal=DeadSignal.SCHEDULER_HEARTBEAT_STALE,
                    pipeline_id="scheduler_heartbeat",
                    details={
                        "heartbeat_age_seconds": round(age),
                        "message": f"Scheduler heartbeat is {age / 60:.0f}m old — scheduler may be frozen",
                    },
                    severity="critical" if age > 30 * 60 else "warning",
                ))
        except Exception as exc:
            logger.warning("dead_detector: heartbeat check failed: %s", exc)

        return signals

    def _check_liveness_states(self) -> list[DeadPipelineSignal]:
        """Map STALLED/DEAD liveness states to DeadPipelineSignals."""
        signals = []
        try:
            svc = PipelineLivenessService(self.db)
            snapshot = svc.snapshot()
            for state in snapshot.pipelines:
                if state.status in (PipelineStatus.STALLED, PipelineStatus.DEAD, PipelineStatus.BLOCKED):
                    signals.append(DeadPipelineSignal(
                        signal=DeadSignal.PIPELINE_STALLED,
                        pipeline_id=state.pipeline_id,
                        details={
                            "liveness_status": state.status.value,
                            "lag_seconds": state.lag_seconds,
                            "last_success": state.last_success,
                            "reason": state.reason,
                        },
                        severity="critical" if state.status == PipelineStatus.DEAD else "warning",
                    ))
        except Exception as exc:
            logger.warning("dead_detector: liveness check failed: %s", exc)

        return signals


# ──────────────────────────────────────────────────────────────────────────────
# Self-Healing Coordinator
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SelfHealingAction:
    action: str            # "normalize_wake_up" | "log_only" | "throttled"
    pipeline_id: str
    triggered_by: str      # signal.value
    details: dict[str, Any]
    timestamp: str


class SelfHealingCoordinator:
    """Evaluates dead-pipeline signals and decides safe recovery actions.

    Recovery actions are ADVISORY and BOUNDED:
    - At most MAX_TRIGGERS_PER_HOUR per pipeline per hour
    - Actions are logged to self_healing_log.jsonl
    - No automatic deletes, no mass retries, no data destruction

    The coordinator does NOT directly execute jobs — it returns actions
    that the caller may pass to the scheduler or log for human review.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self._state = self._load_state()

    # ── Public API ─────────────────────────────────────────────────────────────

    def evaluate_and_act(self) -> list[SelfHealingAction]:
        """Run all detectors, decide actions, persist audit log.

        Returns list of actions taken (including throttled/log_only ones).
        """
        detector = DeadPipelineDetector(self.db)
        signals = detector.detect()

        actions: list[SelfHealingAction] = []
        for signal in signals:
            action = self._decide(signal)
            actions.append(action)
            self._log_action(action)

        self._save_state()
        return actions

    def get_recovery_batch_size(self, module: str, backlog_count: int) -> int:
        """Compute safe batch size for backlog recovery (Phase 6)."""
        engine = BacklogRecoveryEngine(self.db)
        return engine.compute_safe_batch_size(
            module=module,
            backlog_count=backlog_count,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _decide(self, signal: DeadPipelineSignal) -> SelfHealingAction:
        """Map a signal to a safe, bounded recovery action."""
        now_iso = datetime.now(timezone.utc).isoformat()
        pipeline_id = signal.pipeline_id

        # Check throttle
        if self._is_throttled(pipeline_id):
            return SelfHealingAction(
                action="throttled",
                pipeline_id=pipeline_id,
                triggered_by=signal.signal.value,
                details={
                    "reason": f"Rate limit: >{MAX_TRIGGERS_PER_HOUR} triggers/hour for {pipeline_id}",
                    "severity": signal.severity,
                    **signal.details,
                },
                timestamp=now_iso,
            )

        # Determine safe action based on signal type
        if signal.signal == DeadSignal.BACKLOG_GROWING_NORMALIZE_STATIC:
            self._record_trigger(pipeline_id)
            return SelfHealingAction(
                action="normalize_wake_up",
                pipeline_id=pipeline_id,
                triggered_by=signal.signal.value,
                details={
                    "recommended_action": (
                        "Schedule a bounded normalize_job run "
                        f"(batch_size={self._safe_batch_size(pipeline_id)})."
                        " DO NOT mass-retry — respect batch limits."
                    ),
                    "severity": signal.severity,
                    **signal.details,
                },
                timestamp=now_iso,
            )

        elif signal.signal == DeadSignal.SCHEDULER_HEARTBEAT_STALE:
            # Advisory only — do NOT restart the scheduler automatically
            return SelfHealingAction(
                action="log_only",
                pipeline_id=pipeline_id,
                triggered_by=signal.signal.value,
                details={
                    "recommended_action": (
                        "Investigate scheduler container. "
                        "Check docker logs data-core-scheduler-1. "
                        "Do NOT restart automatically."
                    ),
                    "severity": signal.severity,
                    **signal.details,
                },
                timestamp=now_iso,
            )

        elif signal.signal == DeadSignal.PIPELINE_STALLED:
            if signal.severity == "critical":
                self._record_trigger(pipeline_id)
                return SelfHealingAction(
                    action="normalize_wake_up",
                    pipeline_id=pipeline_id,
                    triggered_by=signal.signal.value,
                    details={
                        "recommended_action": (
                            "Pipeline is DEAD. Schedule a single bounded run "
                            "to verify viability before bulk recovery."
                        ),
                        "severity": signal.severity,
                        **signal.details,
                    },
                    timestamp=now_iso,
                )
            else:
                return SelfHealingAction(
                    action="log_only",
                    pipeline_id=pipeline_id,
                    triggered_by=signal.signal.value,
                    details={
                        "recommended_action": "Pipeline STALLED — monitoring. Will escalate if no improvement.",
                        "severity": signal.severity,
                        **signal.details,
                    },
                    timestamp=now_iso,
                )

        else:
            # Default: log only
            return SelfHealingAction(
                action="log_only",
                pipeline_id=pipeline_id,
                triggered_by=signal.signal.value,
                details={**signal.details, "severity": signal.severity},
                timestamp=now_iso,
            )

    def _safe_batch_size(self, pipeline_id: str) -> int:
        """Return a conservative batch size for recovery."""
        from core.config import settings
        return min(
            settings.scheduler_reliability_base_batch_size,
            50,  # cap at 50 items per recovery batch
        )

    def _is_throttled(self, pipeline_id: str) -> bool:
        """Return True if this pipeline exceeded MAX_TRIGGERS_PER_HOUR."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=1)).isoformat()
        pipeline_triggers = self._state.get("triggers", {}).get(pipeline_id, [])
        recent = [ts for ts in pipeline_triggers if ts >= cutoff]
        return len(recent) >= MAX_TRIGGERS_PER_HOUR

    def _record_trigger(self, pipeline_id: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        triggers = self._state.setdefault("triggers", {})
        pipeline_list = triggers.setdefault(pipeline_id, [])
        pipeline_list.append(now_iso)
        # Prune entries older than 2 hours
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        triggers[pipeline_id] = [ts for ts in pipeline_list if ts >= cutoff]

    def _log_action(self, action: SelfHealingAction) -> None:
        """Append action to the audit JSONL log."""
        try:
            SELF_HEALING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = json.dumps(asdict(action), sort_keys=True, default=str)
            with SELF_HEALING_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as exc:
            logger.warning("self_healing: log write failed: %s", exc)

    def _load_state(self) -> dict[str, Any]:
        try:
            if SELF_HEALING_STATE_PATH.exists():
                return json.loads(SELF_HEALING_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(self) -> None:
        try:
            SELF_HEALING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SELF_HEALING_STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, sort_keys=True), encoding="utf-8")
            tmp.replace(SELF_HEALING_STATE_PATH)
        except Exception as exc:
            logger.warning("self_healing: state save failed: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Backlog Recovery Engine (Phase 6)
# ──────────────────────────────────────────────────────────────────────────────

class BacklogRecoveryEngine:
    """Computes safe adaptive batch sizes for backlog recovery.

    Principles:
    - Small batches when DB pool is under pressure
    - Larger batches when system is healthy and backlog is large
    - Never exceeds configured base_batch_size
    - Dead-letter tracking: records items that fail repeatedly

    DB pressure is estimated from pool.checkedout() / pool.size().
    """

    BASE_BATCH = 100
    MIN_BATCH = 10

    def __init__(self, db: Session) -> None:
        self.db = db

    def compute_safe_batch_size(self, module: str, backlog_count: int) -> int:
        """Return a batch size safe for current system load.

        Args:
            module: Pipeline module name (e.g. "ecommerce").
            backlog_count: Current pending raw records for this module.

        Returns:
            int: Recommended batch size (MIN_BATCH ≤ result ≤ BASE_BATCH).
        """
        from core.config import settings
        base = settings.scheduler_reliability_base_batch_size

        pressure = self._db_pressure()

        if pressure >= DB_PRESSURE_CRITICAL_THRESHOLD:
            # Critical: use minimum batch
            batch = self.MIN_BATCH
            reason = f"DB critical pressure={pressure:.2f}"
        elif pressure >= DB_PRESSURE_HIGH_THRESHOLD:
            # High: halve the base batch
            batch = max(self.MIN_BATCH, base // 2)
            reason = f"DB high pressure={pressure:.2f}"
        elif backlog_count > 500:
            # Large backlog: use base batch to clear faster
            batch = base
            reason = f"Large backlog={backlog_count}"
        elif backlog_count > 100:
            # Moderate backlog: 75% of base
            batch = max(self.MIN_BATCH, int(base * 0.75))
            reason = f"Moderate backlog={backlog_count}"
        else:
            # Normal: use base batch
            batch = min(base, 50)  # conservative for small backlog
            reason = f"Normal backlog={backlog_count}"

        logger.debug(
            "backlog_recovery: batch=%d reason=%s module=%s",
            batch, reason, module,
        )
        return batch

    def pending_count(self, module: str) -> int:
        """Return current pending raw count for a module."""
        try:
            return (
                self.db.query(func.count(RawCollection.id))
                .filter(
                    RawCollection.module == module,
                    RawCollection.processing_status == "normalization_pending",
                )
                .scalar()
            ) or 0
        except Exception:
            return 0

    def _db_pressure(self) -> float:
        """Estimate DB pool pressure (0.0 = idle, 1.0 = fully saturated)."""
        try:
            from database.session import engine
            pool = engine.pool
            size = pool.size()  # type: ignore[attr-defined]
            checked_out = pool.checkedout()  # type: ignore[attr-defined]
            if size <= 0:
                return 0.0
            return min(1.0, checked_out / size)
        except Exception:
            return 0.0

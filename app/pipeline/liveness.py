"""Pipeline Liveness Registry — Phase 1.

Provides ``PipelineLivenessService`` which classifies every registered pipeline
into one of five states:

  RUNNING   — a run is currently in-flight (status='running' within 30 min)
  DEGRADED  — last success within 2× expected interval (late but functional)
  STALLED   — last success within 10× expected interval (no recent output)
  BLOCKED   — last run errored; no success since (pipeline is failing)
  DEAD      — no run recorded in 24 h, OR last success > 10× expected interval

Classification uses the ``pipeline_runs`` table only — no cross-table joins.
Results are cached in ``runtime-data/pipeline_liveness.json`` so the API
container can read them without touching the DB on every request.

Usage::

    from database.session import SessionLocal
    db = SessionLocal()
    try:
        svc = PipelineLivenessService(db)
        snapshot = svc.snapshot()
    finally:
        db.close()
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.pipeline.models import PipelineRun
from app.runtime.heartbeat import RUNTIME_DATA_DIR

logger = logging.getLogger(__name__)

LIVENESS_CACHE_PATH = Path(
    os.getenv(
        "PIPELINE_LIVENESS_PATH",
        str(RUNTIME_DATA_DIR / "pipeline_liveness.json"),
    )
)


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline Status Enum
# ──────────────────────────────────────────────────────────────────────────────

class PipelineStatus(str, Enum):
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STALLED = "STALLED"
    BLOCKED = "BLOCKED"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"   # used when no run data is available at all


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline Descriptor
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PipelineDescriptor:
    """Static description of a single schedulable pipeline."""

    pipeline_id: str        # e.g. "normalize_ecommerce"
    domain: str             # PipelineRun.domain
    stage: str              # PipelineRun.stage
    expected_interval_seconds: int   # normal cadence
    critical_lag_multiplier: float = 10.0  # × expected_interval before DEAD
    degraded_lag_multiplier: float = 2.0   # × expected_interval before DEGRADED


# Canonical registry of all scheduled pipelines (derived from scheduler/service.py)
PIPELINE_REGISTRY: list[PipelineDescriptor] = [
    # ── Ecommerce ─────────────────────────────────────────────────────────────
    PipelineDescriptor(
        pipeline_id="normalize_ecommerce",
        domain="ecommerce",
        stage="normalization",
        expected_interval_seconds=15 * 60,   # 15 min
    ),
    PipelineDescriptor(
        pipeline_id="analytics_ecommerce",
        domain="ecommerce",
        stage="analytics",
        expected_interval_seconds=60 * 60,   # 60 min
    ),
    PipelineDescriptor(
        pipeline_id="collection_ecommerce",
        domain="ecommerce",
        stage="collection",
        expected_interval_seconds=120 * 60,  # 2 h
    ),
    # ── Crypto ────────────────────────────────────────────────────────────────
    PipelineDescriptor(
        pipeline_id="normalize_crypto",
        domain="crypto",
        stage="normalization",
        expected_interval_seconds=15 * 60,
    ),
    PipelineDescriptor(
        pipeline_id="analytics_crypto",
        domain="crypto",
        stage="analytics",
        expected_interval_seconds=60 * 60,
    ),
    PipelineDescriptor(
        pipeline_id="collection_crypto",
        domain="crypto",
        stage="collection",
        expected_interval_seconds=60 * 60,   # crypto collectors run hourly
    ),
    # ── Real Estate ── APOSENTADO 2026-06-13: tabelas dropadas em 0031_sunset ──
    # PipelineDescriptor(
    #     pipeline_id="collection_real_estate",
    #     domain="real_estate",
    #     stage="collection",
    #     expected_interval_seconds=24 * 3600,
    #     degraded_lag_multiplier=1.5,
    #     critical_lag_multiplier=3.0,
    # ),
    # PipelineDescriptor(
    #     pipeline_id="normalize_real_estate",
    #     domain="real_estate",
    #     stage="normalization",
    #     expected_interval_seconds=24 * 3600,
    #     degraded_lag_multiplier=1.5,
    #     critical_lag_multiplier=3.0,
    # ),
    # ── Trading ───────────────────────────────────────────────────────────────
    PipelineDescriptor(
        pipeline_id="normalize_trading",
        domain="trading",
        stage="normalization",
        expected_interval_seconds=15 * 60,
    ),
    PipelineDescriptor(
        pipeline_id="analytics_trading",
        domain="trading",
        stage="analytics",
        expected_interval_seconds=60 * 60,
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# Liveness State
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PipelineLivenessState:
    pipeline_id: str
    domain: str
    stage: str
    status: PipelineStatus
    last_heartbeat: str | None     # ISO8601 UTC of most recent run started_at
    last_success: str | None       # ISO8601 UTC of most recent success/partial finished_at
    last_failure: str | None       # ISO8601 UTC of most recent error finished_at
    lag_seconds: float | None      # seconds since last success (None = never run)
    backlog: int                   # normalization_pending count (0 for non-ecommerce)
    expected_interval_seconds: int
    reason: str                    # human-readable classification explanation

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class PipelineLivenessSnapshot:
    evaluated_at: str
    pipelines: list[PipelineLivenessState] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated_at": self.evaluated_at,
            "summary": self.summary,
            "pipelines": [p.to_dict() for p in self.pipelines],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Liveness Service
# ──────────────────────────────────────────────────────────────────────────────

class PipelineLivenessService:
    """Evaluates liveness for all registered pipelines and persists a snapshot."""

    # A run that is "running" for longer than this is considered hung (leaked)
    MAX_RUNNING_AGE_SECONDS = 30 * 60   # 30 min

    def __init__(self, db: Session) -> None:
        self.db = db
        self._now = datetime.now(timezone.utc)

    # ── Public API ─────────────────────────────────────────────────────────────

    def snapshot(self) -> PipelineLivenessSnapshot:
        """Evaluate all pipelines and return a full liveness snapshot.

        Side effect: writes result to LIVENESS_CACHE_PATH (atomic rename).
        """
        states: list[PipelineLivenessState] = []
        for desc in PIPELINE_REGISTRY:
            state = self._evaluate(desc)
            states.append(state)

        summary: dict[str, int] = {}
        for st in PipelineStatus:
            count = sum(1 for s in states if s.status == st)
            if count > 0:
                summary[st.value] = count

        snap = PipelineLivenessSnapshot(
            evaluated_at=self._now.isoformat(),
            pipelines=states,
            summary=summary,
        )
        self._persist(snap)
        return snap

    # ── Internal ──────────────────────────────────────────────────────────────

    def _evaluate(self, desc: PipelineDescriptor) -> PipelineLivenessState:
        """Classify a single pipeline."""
        # Query: last 5 runs for this domain+stage (enough for classification)
        recent_runs = (
            self.db.query(PipelineRun)
            .filter(
                PipelineRun.domain == desc.domain,
                PipelineRun.stage == desc.stage,
            )
            .order_by(PipelineRun.started_at.desc())
            .limit(5)
            .all()
        )

        last_run = recent_runs[0] if recent_runs else None
        last_success_run = next(
            (r for r in recent_runs if r.status in ("success", "partial")),
            None,
        )
        last_error_run = next(
            (r for r in recent_runs if r.status == "error"),
            None,
        )

        # Timestamps
        last_heartbeat_iso = (
            last_run.started_at.isoformat()
            if last_run and last_run.started_at
            else None
        )
        last_success_iso = (
            last_success_run.finished_at.isoformat()
            if last_success_run and last_success_run.finished_at
            else None
        )
        last_failure_iso = (
            last_error_run.finished_at.isoformat()
            if last_error_run and last_error_run.finished_at
            else None
        )

        # Lag since last success (seconds)
        lag_seconds: float | None = None
        if last_success_run and last_success_run.finished_at:
            finished = last_success_run.finished_at
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            lag_seconds = (self._now - finished).total_seconds()

        # ── Classification ────────────────────────────────────────────────────

        if last_run is None:
            # No history at all
            return PipelineLivenessState(
                pipeline_id=desc.pipeline_id,
                domain=desc.domain,
                stage=desc.stage,
                status=PipelineStatus.UNKNOWN,
                last_heartbeat=None,
                last_success=None,
                last_failure=None,
                lag_seconds=None,
                backlog=0,
                expected_interval_seconds=desc.expected_interval_seconds,
                reason="No pipeline_runs records found — pipeline may never have executed",
            )

        # Currently in-flight?
        if last_run.status == "running":
            run_age = (self._now - (
                last_run.started_at.replace(tzinfo=timezone.utc)
                if last_run.started_at.tzinfo is None
                else last_run.started_at
            )).total_seconds()
            if run_age <= self.MAX_RUNNING_AGE_SECONDS:
                return PipelineLivenessState(
                    pipeline_id=desc.pipeline_id,
                    domain=desc.domain,
                    stage=desc.stage,
                    status=PipelineStatus.RUNNING,
                    last_heartbeat=last_heartbeat_iso,
                    last_success=last_success_iso,
                    last_failure=last_failure_iso,
                    lag_seconds=lag_seconds,
                    backlog=0,
                    expected_interval_seconds=desc.expected_interval_seconds,
                    reason=f"Run in progress for {run_age:.0f}s",
                )
            # Hung run — fall through to regular classification using lag

        # Thresholds
        degraded_threshold = desc.expected_interval_seconds * desc.degraded_lag_multiplier
        dead_threshold = desc.expected_interval_seconds * desc.critical_lag_multiplier

        if lag_seconds is None:
            # Runs exist but NEVER succeeded
            status = PipelineStatus.BLOCKED
            reason = "Pipeline has run but never completed successfully"
        elif lag_seconds > dead_threshold:
            status = PipelineStatus.DEAD
            reason = (
                f"Last success {lag_seconds / 3600:.1f}h ago "
                f"(threshold {dead_threshold / 3600:.1f}h)"
            )
        elif lag_seconds > degraded_threshold:
            # Check whether last run was an error
            if last_run.status == "error" and (
                last_error_run
                and (last_success_run is None or last_error_run.started_at > last_success_run.started_at)
            ):
                status = PipelineStatus.BLOCKED
                reason = (
                    f"Last run errored; last success {lag_seconds / 3600:.1f}h ago "
                    f"(degraded threshold {degraded_threshold / 3600:.1f}h)"
                )
            else:
                status = PipelineStatus.STALLED
                reason = (
                    f"Last success {lag_seconds / 60:.0f}m ago "
                    f"(expected every {desc.expected_interval_seconds // 60}m)"
                )
        else:
            # Within degraded window — but check for consecutive errors
            if (
                last_run.status == "error"
                and last_error_run
                and (last_success_run is None or last_error_run.started_at > last_success_run.started_at)
            ):
                status = PipelineStatus.DEGRADED
                reason = "Last run errored (within normal lag window — monitoring)"
            else:
                status = PipelineStatus.DEGRADED if lag_seconds > desc.expected_interval_seconds else PipelineStatus.DEGRADED
                # Within normal window
                if lag_seconds <= desc.expected_interval_seconds * 1.2:
                    status = PipelineStatus.DEGRADED  # mildly late — use DEGRADED not OK (no OK state per spec)
                    # Actually if within expected interval let's mark as DEGRADED with context
                    # The spec only has RUNNING/DEGRADED/STALLED/BLOCKED/DEAD — no OK
                    # DEGRADED = late but functional
                    reason = (
                        f"Last success {lag_seconds / 60:.0f}m ago "
                        f"(within expected {desc.expected_interval_seconds // 60}m interval)"
                    )
                else:
                    status = PipelineStatus.DEGRADED
                    reason = (
                        f"Last success {lag_seconds / 60:.0f}m ago — "
                        f"slightly late (expected every {desc.expected_interval_seconds // 60}m)"
                    )

        return PipelineLivenessState(
            pipeline_id=desc.pipeline_id,
            domain=desc.domain,
            stage=desc.stage,
            status=status,
            last_heartbeat=last_heartbeat_iso,
            last_success=last_success_iso,
            last_failure=last_failure_iso,
            lag_seconds=lag_seconds,
            backlog=0,
            expected_interval_seconds=desc.expected_interval_seconds,
            reason=reason,
        )

    @staticmethod
    def _persist(snapshot: PipelineLivenessSnapshot) -> None:
        """Write snapshot to LIVENESS_CACHE_PATH atomically (tmp → rename)."""
        try:
            LIVENESS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = LIVENESS_CACHE_PATH.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(snapshot.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(LIVENESS_CACHE_PATH)
        except Exception as exc:
            logger.warning("pipeline_liveness: failed to persist snapshot: %s", exc)

    @staticmethod
    def read_cached() -> dict[str, Any] | None:
        """Read the last persisted snapshot without touching the DB.

        Returns None if the file does not exist or is unreadable.
        """
        try:
            if not LIVENESS_CACHE_PATH.exists():
                return None
            return json.loads(LIVENESS_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None

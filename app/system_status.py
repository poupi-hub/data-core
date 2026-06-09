from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, text
from sqlalchemy.orm import Session

from app.analytics.models import ProductPriceAnalytics, TradingAnalytics
from app.normalization.models import (
    NormalizedMarketCandle,
    NormalizedProduct,
)
from app.pipeline.liveness import PipelineLivenessService
from app.pipeline.models import PipelineRun
from app.raw.models import RawCollection
from app.runtime.heartbeat import read_worker_heartbeat
from app.runtime.scheduler_heartbeat import heartbeat_age_seconds, read_scheduler_heartbeat
from app.runtime.scheduler_watchdog import DataCoreSchedulerWatchdog
from core.config import settings
from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus
from database.session import get_db

router = APIRouter(tags=["operational-readiness"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((now - value).total_seconds()))


def _state_from_blockers(blockers: list[str], degraded: list[str]) -> str:
    if any(item.startswith("infra:") for item in blockers):
        return "NO-GO"
    if blockers:
        return "BLOCKED"
    if degraded:
        return "DEGRADED"
    return "READY"


def _worker_status(now: datetime) -> dict[str, Any]:
    heartbeat = read_worker_heartbeat()
    age = None
    active = False
    if heartbeat:
        age = _age_seconds(
            datetime.fromtimestamp(float(heartbeat.get("timestamp_epoch") or 0), tz=timezone.utc),
            now,
        )
        active = (
            heartbeat.get("status") != "stopped"
            and age is not None
            and age <= max(settings.worker_pipeline_interval_seconds * 2, 180)
        )
    return {
        "active": active,
        "status": heartbeat.get("status") if heartbeat else "missing",
        "heartbeat_age_seconds": age,
        "heartbeat": heartbeat,
        "required": True,
    }


def _dependency_status(db: Session) -> dict[str, Any]:
    deps: dict[str, Any] = {}
    try:
        db.execute(text("SELECT 1"))
        deps["postgres"] = {"status": "READY"}
    except Exception as exc:
        deps["postgres"] = {"status": "NO-GO", "error": str(exc)}

    redis = {"status": "ADVISORY_ONLY", "used": settings.cache_enabled}
    try:
        import redis as redis_lib

        client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        redis["up"] = True
        redis["status"] = "READY" if settings.cache_enabled else "ADVISORY_ONLY"
    except Exception as exc:
        redis["up"] = False
        redis["error"] = str(exc)
        redis["status"] = "NO-GO" if settings.cache_enabled else "ADVISORY_ONLY"
    deps["redis"] = redis
    return deps


def _freshness_status(db: Session, now: datetime) -> dict[str, Any]:
    rows = (
        db.query(
            RawCollection.module,
            RawCollection.source_name,
            func.count(RawCollection.id),
            func.max(RawCollection.collected_at),
        )
        .group_by(RawCollection.module, RawCollection.source_name)
        .all()
    )
    items = []
    for module, source_name, count, latest in rows:
        age = _age_seconds(latest, now)
        items.append(
            {
                "module": module,
                "source_name": source_name,
                "raw_count": int(count or 0),
                "latest_collected_at": latest,
                "age_seconds": age,
                "status": "READY" if age is not None and age <= 3600 else "DEGRADED",
            }
        )
    return {"items": sorted(items, key=lambda item: (item["module"], item["source_name"] or ""))}


def _pipeline_lag(db: Session, now: datetime) -> dict[str, Any]:
    raw_pending_total = db.query(RawCollection).filter(RawCollection.processing_status == "normalization_pending").count()
    raw_failed_total = db.query(RawCollection).filter(RawCollection.processing_status == "normalization_failed").count()
    crypto_pending = (
        db.query(RawCollection)
        .filter(RawCollection.module == "crypto", RawCollection.processing_status == "normalization_pending")
        .count()
    )
    latest_crypto_raw = (
        db.query(func.max(RawCollection.collected_at))
        .filter(RawCollection.module == "crypto")
        .scalar()
    )
    latest_crypto_normalized = db.query(func.max(NormalizedMarketCandle.normalized_at)).scalar()
    latest_crypto_analytics = db.query(func.max(TradingAnalytics.calculated_at)).scalar()
    latest_product_normalized = db.query(func.max(NormalizedProduct.normalized_at)).scalar()
    latest_product_analytics = db.query(func.max(ProductPriceAnalytics.calculated_at)).scalar()

    return {
        "raw_pending_total": raw_pending_total,
        "raw_failed_total": raw_failed_total,
        "crypto_raw_pending": crypto_pending,
        "normalization": {
            "crypto_latest_raw_at": latest_crypto_raw,
            "crypto_latest_normalized_at": latest_crypto_normalized,
            "crypto_lag_seconds": _lag_between(latest_crypto_raw, latest_crypto_normalized),
            "product_latest_normalized_at": latest_product_normalized,
            "last_success_at": _latest_pipeline_success(db, "normalization"),
        },
        "analytics": {
            "crypto_latest_analytics_at": latest_crypto_analytics,
            "crypto_lag_seconds": _lag_between(latest_crypto_raw, latest_crypto_analytics),
            "product_latest_analytics_at": latest_product_analytics,
            "last_success_at": _latest_pipeline_success(db, "analytics"),
        },
        "ages": {
            "crypto_latest_raw_age_seconds": _age_seconds(latest_crypto_raw, now),
            "crypto_latest_normalized_age_seconds": _age_seconds(latest_crypto_normalized, now),
            "crypto_latest_analytics_age_seconds": _age_seconds(latest_crypto_analytics, now),
        },
    }


def _lag_between(newer: datetime | None, older: datetime | None) -> int | None:
    if newer is None or older is None:
        return None
    if newer.tzinfo is None:
        newer = newer.replace(tzinfo=timezone.utc)
    if older.tzinfo is None:
        older = older.replace(tzinfo=timezone.utc)
    return max(0, int((newer - older).total_seconds()))


def _latest_pipeline_success(db: Session, stage: str) -> datetime | None:
    return (
        db.query(func.max(PipelineRun.finished_at))
        .filter(PipelineRun.stage == stage, PipelineRun.status.in_(["success", "partial"]))
        .scalar()
    )


def _provider_status(db: Session, now: datetime) -> dict[str, Any]:
    sources = ["paguemenos", "drogasil", "drogaraia"]
    providers = []
    for source_name in sources:
        active_targets = (
            db.query(CollectionTarget)
            .filter(
                CollectionTarget.module == "ecommerce",
                CollectionTarget.source_name == source_name,
                CollectionTarget.active.is_(True),
            )
            .count()
        )
        latest_raw = (
            db.query(RawCollection)
            .filter(RawCollection.module == "ecommerce", RawCollection.source_name == source_name)
            .order_by(desc(RawCollection.collected_at))
            .first()
        )
        latest_product_at = (
            db.query(func.max(NormalizedProduct.normalized_at))
            .filter(NormalizedProduct.store_name == source_name)
            .scalar()
        )
        latest_analytics_at = (
            db.query(func.max(ProductPriceAnalytics.calculated_at))
            .join(NormalizedProduct, ProductPriceAnalytics.product_id == NormalizedProduct.id)
            .filter(NormalizedProduct.store_name == source_name)
            .scalar()
        )
        latest_error = latest_raw.error_message if latest_raw else None
        raw_age = _age_seconds(latest_raw.collected_at if latest_raw else None, now)
        healthy = latest_product_at is not None and latest_analytics_at is not None and raw_age is not None and raw_age <= 86400
        blocked = bool(latest_error) and "HTTP_403_FORBIDDEN" in latest_error
        providers.append(
            {
                "provider": source_name,
                "status": "READY" if healthy else "BLOCKED" if blocked else "DEGRADED",
                "active_targets": active_targets,
                "latest_raw_at": latest_raw.collected_at if latest_raw else None,
                "latest_success_at": latest_product_at,
                "latest_analytics_at": latest_analytics_at,
                "last_error": latest_error,
                "freshness_age_seconds": raw_age,
            }
        )
    return {
        "provider_count_total": len(providers),
        "provider_count_healthy": sum(1 for item in providers if item["status"] == "READY"),
        "provider_count_blocked": sum(1 for item in providers if item["status"] == "BLOCKED"),
        "providers": providers,
    }


def _scheduler_heartbeat_status(now: datetime) -> dict[str, Any]:
    """Return scheduler proof-of-execution heartbeat summary (Phase 2).

    Reads from the runtime-data shared volume — no DB required.
    """
    hb = read_scheduler_heartbeat()
    if hb is None:
        return {
            "status": "MISSING",
            "heartbeat_age_seconds": None,
            "last_job": None,
            "last_job_at": None,
            "last_job_status": None,
            "last_success_job": None,
            "last_success_at": None,
            "last_failure_job": None,
            "consecutive_failures": None,
            "execution_drift_seconds": None,
            "jobs_executed_total": None,
            "scheduler_started_at": None,
            "warning": "No scheduler heartbeat file found. Scheduler may have never started.",
        }

    age = heartbeat_age_seconds()
    if age is None:
        status = "MISSING"
    elif age > 30 * 60:
        status = "DEAD"      # No heartbeat for 30+ min
    elif age > 10 * 60:
        status = "STALLED"   # No heartbeat for 10+ min
    else:
        status = "ALIVE"

    return {
        "status": status,
        "heartbeat_age_seconds": round(age) if age is not None else None,
        "last_job": hb.get("last_job"),
        "last_job_at": hb.get("last_job_at"),
        "last_job_status": hb.get("last_job_status"),
        "last_success_job": hb.get("last_success_job"),
        "last_success_at": hb.get("last_success_at"),
        "last_failure_job": hb.get("last_failure_job"),
        "last_failure_at": hb.get("last_failure_at"),
        "consecutive_failures": hb.get("consecutive_failures", 0),
        "execution_drift_seconds": hb.get("execution_drift_seconds"),
        "jobs_executed_total": hb.get("jobs_executed_total", 0),
        "scheduler_started_at": hb.get("scheduler_started_at"),
        "pid": hb.get("pid"),
    }


def _pipeline_liveness_status(db: Session) -> dict[str, Any]:
    """Return pipeline liveness snapshot (Phase 1).

    Reads from runtime-data cache if available; falls back to live DB eval.
    """
    # Try cache first (API container avoids extra DB load)
    cached = PipelineLivenessService.read_cached()
    if cached is not None:
        return {
            "source": "cache",
            **cached,
        }

    # Fallback: evaluate live from DB
    try:
        svc = PipelineLivenessService(db)
        snapshot = svc.snapshot()
        return {
            "source": "live_eval",
            **snapshot.to_dict(),
        }
    except Exception as exc:
        return {
            "source": "error",
            "error": str(exc),
            "pipelines": [],
            "summary": {},
        }


def _collection_readiness_summary(db: Session) -> dict[str, Any]:
    active_targets = db.query(CollectionTarget).filter(CollectionTarget.active.is_(True)).count()
    raw_pending = db.query(RawCollection).filter(RawCollection.processing_status == "normalization_pending").count()
    raw_failed = db.query(RawCollection).filter(RawCollection.processing_status == "normalization_failed").count()
    unresolved_errors = db.query(CollectorError).filter(CollectorError.resolved_at.is_(None)).all()
    blocking_errors = 0
    recovered_errors = 0
    for error in unresolved_errors:
        success_after_error = (
            db.query(CollectionRun.id)
            .filter(
                CollectionRun.collector_name == error.collector_name,
                CollectionRun.status == RunStatus.success,
                CollectionRun.finished_at.is_not(None),
                CollectionRun.finished_at >= error.created_at,
            )
            .first()
        )
        if success_after_error:
            recovered_errors += 1
        else:
            blocking_errors += 1
    return {
        "active_targets": active_targets,
        "raw_pending": raw_pending,
        "raw_failed": raw_failed,
        "unresolved_collector_errors": len(unresolved_errors),
        "blocking_collector_errors": blocking_errors,
        "recovered_unresolved_collector_errors": recovered_errors,
        "ready": active_targets > 0 and raw_pending == 0 and raw_failed == 0 and blocking_errors == 0,
    }


def build_system_status(db: Session) -> dict[str, Any]:
    now = _now()
    deps = _dependency_status(db)
    worker = _worker_status(now)
    scheduler_watchdog = DataCoreSchedulerWatchdog().diagnose(db).to_dict()
    scheduler_hb = _scheduler_heartbeat_status(now)        # Phase 2
    pipeline_liveness = _pipeline_liveness_status(db)     # Phase 1
    freshness = _freshness_status(db, now)
    pipeline = _pipeline_lag(db, now)
    providers = _provider_status(db, now)
    collection_readiness = _collection_readiness_summary(db)
    latest_runs = (
        db.query(CollectionRun)
        .order_by(desc(CollectionRun.started_at), desc(CollectionRun.created_at))
        .limit(5)
        .all()
    )

    blockers: list[str] = []
    degraded: list[str] = []
    if deps["postgres"]["status"] != "READY":
        blockers.append("infra:postgres_unavailable")
    if deps["redis"]["status"] == "NO-GO":
        blockers.append("infra:redis_unavailable")
    if not worker["active"]:
        blockers.append("worker_absent_or_stale")
    if scheduler_watchdog["operational_state"] == "SCHEDULER_RESTART_LOOP":
        blockers.append("scheduler_restart_loop")
    elif scheduler_watchdog["alert_severity"] == "critical":
        blockers.append("scheduler_critical")
    elif scheduler_watchdog["operational_state"] != "SCHEDULER_HEALTHY":
        degraded.append("scheduler_degraded")

    # ── Phase 2: Heartbeat-based stall detection ──────────────────────────────
    hb_status = scheduler_hb.get("status")
    if hb_status == "DEAD":
        blockers.append("scheduler_heartbeat_dead")
    elif hb_status in ("STALLED", "MISSING"):
        degraded.append("scheduler_heartbeat_stale")

    # ── Phase 1: Pipeline liveness stall signals ──────────────────────────────
    liveness_summary = pipeline_liveness.get("summary", {})
    if liveness_summary.get("DEAD", 0) > 0:
        blockers.append("pipeline_liveness_dead")
    if liveness_summary.get("BLOCKED", 0) > 0:
        degraded.append("pipeline_liveness_blocked")
    if liveness_summary.get("STALLED", 0) > 0:
        degraded.append("pipeline_liveness_stalled")

    if pipeline["crypto_raw_pending"] > 500:
        blockers.append("crypto_normalization_backlog_critical")
    elif pipeline["crypto_raw_pending"] > 0:
        degraded.append("crypto_normalization_backlog")
    if pipeline["normalization"]["crypto_lag_seconds"] is None or pipeline["normalization"]["crypto_lag_seconds"] > 7200:
        blockers.append("crypto_normalization_stale")
    if pipeline["analytics"]["crypto_lag_seconds"] is None or pipeline["analytics"]["crypto_lag_seconds"] > 7200:
        blockers.append("crypto_analytics_stale")
    if providers["provider_count_healthy"] == 0:
        blockers.append("no_ecommerce_provider_healthy")
    elif providers["provider_count_blocked"] > 0:
        degraded.append("ecommerce_providers_blocked")
    if not collection_readiness["ready"]:
        degraded.append("collection_readiness_false")

    status = _state_from_blockers(blockers, degraded)
    decision = "NO-GO" if status in {"NO-GO", "BLOCKED"} else status
    return {
        "generated_at": now,
        "environment": settings.app_env,
        "status": status,
        "decision": decision,
        "blockers": blockers,
        "degraded": degraded,
        "runtime": {
            "api": "READY",
            "dependencies": deps,
            # Combined scheduler view: Phase 9 merges watchdog + heartbeat
            "scheduler": {
                **scheduler_watchdog,
                # Phase 2: inject proof-of-execution heartbeat
                "heartbeat": scheduler_hb,
            },
            "worker": worker,
        },
        # Phase 1: pipeline liveness registry
        "pipelines": pipeline_liveness,
        "queues": {
            "bullmq": {
                "status": "ADVISORY_ONLY",
                "used_by_data_core": False,
                "note": "data-core usa backlog em Postgres; BullMQ pertence ao runtime Poupi Baby.",
            },
            # Phase 4: queue lag from DB
            "normalization_backlog": {
                "ecommerce": pipeline.get("raw_pending_total", 0),
                "details_by_module": "see /metrics queue_backlog_total gauge",
            },
        },
        "freshness": freshness,
        "pipeline": pipeline,
        "collection_readiness": collection_readiness,
        "ecommerce": providers,
        "telegram_pipeline": {
            "status": "ADVISORY_ONLY",
            "live_publishing_unchanged": True,
        },
        "prometheus_metric_health": {
            "status": "DEGRADED" if not worker["active"] else "PARTIAL_READY",
            "db_backed_operational_metrics": True,
            "in_process_pipeline_metrics_trusted": False,
            "new_phase8_metrics": [
                "pipeline_liveness_status",
                "pipeline_liveness_lag_seconds",
                "scheduler_heartbeat_age_seconds",
                "scheduler_consecutive_failures",
                "queue_backlog_total",
                "queue_lag_seconds",
            ],
        },
        "redis_cache_truth": {
            "redis_up": deps["redis"].get("up", False),
            "redis_used": settings.cache_enabled,
            "readiness_requires_redis": settings.cache_enabled,
            "lock_strategy": "postgres_collection_runs",
        },
        "recent_collection_runs": [
            {
                "collector_name": run.collector_name,
                "source_name": run.source_name,
                "status": run.status.value if hasattr(run.status, "value") else str(run.status),
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "raw_saved_count": run.raw_saved_count,
                "error_count": run.error_count,
            }
            for run in latest_runs
        ],
    }


@router.get("/system-status")
def system_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return build_system_status(db)


@router.get("/health/business")
def business_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    status = build_system_status(db)
    return {
        "status": status["status"],
        "decision": status["decision"],
        "blockers": status["blockers"],
        "degraded": status["degraded"],
        "generated_at": status["generated_at"],
    }

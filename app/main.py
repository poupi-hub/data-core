import os
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

import api.live_metrics  # noqa: F401 — registers Phase Q Prometheus Gauges in this process
import app.incident_bus.models  # noqa: F401 — ensure incident_events table is registered
import app.incident_history.models  # noqa: F401 — ensure incident_history + incident_patterns tables registered
import app.modules.crypto.edge.alert_state_model  # noqa: F401 — ensure edge_alert_state table registered
import app.modules.crypto.edge.forward_model  # noqa: F401 — ensure forward_shadow_signals table registered
import app.modules.crypto.edge.models  # noqa: F401 — ensure trading_edge_outcomes table registered — ensure TradingSignalOutcome table registered
import app.modules.nba.models  # noqa: F401 — ensure NBA tables registered
import app.modules.nba.quant.models  # noqa: F401 — ensure NBA quant tables registered
import app.modules.trading.validation.models  # noqa: F401
import app.scrapers.models  # noqa: F401 — ensure ScraperDriftEvent table is registered
import app.watchdog.models  # noqa: F401 — ensure WatchdogRun + TelegramPublicationEvent registered
from api.auth import verify_api_key
from api.live_metrics_updater import refresh_live_metrics
from api.poupi_baby_routes import router as poupi_baby_router
from api.rate_limit import limiter
from api.routes import router as api_router
from api.schemas import DependencyStatus, HealthResponse
from app.adaptive_intelligence.api import router as adaptive_intelligence_router
from app.adaptive_policy.api import router as adaptive_policy_router
from app.alerts.api import router as alerts_router
from app.analytics import models as analytics_models
from app.data_quality import models as data_quality_models
from app.data_quality.api import router as data_quality_router
from app.documentation import models as documentation_models
from app.documentation.api import router as documentation_router
from app.incident_bus.router import router as incident_bus_router
from app.incident_history.router import router as incident_history_router
from app.middleware.correlation import CorrelationMiddleware
from app.modules.crypto.api import router as crypto_router
from app.modules.crypto.edge.api import router as edge_router
from app.modules.nba.api import router as nba_router
from app.modules.nba.quant.api import router as nba_quant_router
from app.modules.real_estate import models as real_estate_models
from app.modules.real_estate.api import router as real_estate_router
from app.modules.registry import register_pipeline_modules
from app.modules.sports_odds import models as sports_odds_models
from app.modules.sports_odds.api import router as sports_odds_router
from app.modules.trading.validation.api import router as trading_validation_router
from app.normalization import models as normalization_models
from app.operational_truth.api import router as operational_truth_router
from app.operational_truth.policy.api import router as operational_policy_router
from app.pipeline import models as pipeline_models  # ensure tables are registered
from app.pipeline_api import router as pipeline_router
from app.raw import models as raw_models
from app.runtime.api import router as runtime_router
from app.scrapers.api import router as scrapers_router
from app.system_status import build_system_status
from app.system_status import router as system_status_router
from app.watchdog.api import router as watchdog_router
from core.config import settings
from database.models import Base
from database.session import SessionLocal, engine
from logs.config import configure_logging
from scheduler.service import create_scheduler, start_scheduler, stop_scheduler

_ = real_estate_models
_ = sports_odds_models
_ = raw_models
_ = normalization_models
_ = analytics_models
_ = data_quality_models
_ = documentation_models
_ = pipeline_models
_ = app.scrapers.models
_ = app.watchdog.models
_ = app.modules.trading.validation.models
_ = app.modules.nba.models
_ = app.modules.nba.quant.models


def _metrics_refresh_loop(stop_event: threading.Event, interval: int = 60) -> None:
    """Daemon thread: lê JSONLs Phase O/P/Q e atualiza Gauges Prometheus a cada N segundos."""
    while not stop_event.wait(interval):
        try:
            refresh_live_metrics()
        except Exception:
            pass  # nunca deixar o daemon morrer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = create_scheduler()
        app.state.scheduler = scheduler
        start_scheduler(scheduler)

    # Daemon thread: atualiza Gauges Phase O/P/Q a partir dos JSONLs locais.
    # Necessário porque scripts CLI rodam em processos separados — sem isto,
    # os Gauges ficam em 0 mesmo após execucoes dos modulos de pesquisa.
    _stop = threading.Event()
    _t = threading.Thread(target=_metrics_refresh_loop, args=(_stop,), daemon=True, name="metrics-refresh")  # noqa: E501
    _t.start()
    refresh_live_metrics()  # refresh imediato na startup

    try:
        yield
    finally:
        if scheduler is not None:
            stop_scheduler(scheduler)


def create_app() -> FastAPI:
    register_pipeline_modules()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    # ── Middleware ─────────────────────────────────────────────────────────────
    app.add_middleware(CorrelationMiddleware)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── /health  — full dependency check ──────────────────────────────────────
    @app.get("/health", response_model=HealthResponse, tags=["observability"])
    def health() -> HealthResponse:
        from cache.client import get_redis

        deps: dict[str, DependencyStatus] = {}

        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            deps["postgres"] = DependencyStatus(status="ok")
        except Exception as exc:
            deps["postgres"] = DependencyStatus(status="error", detail=str(exc))

        if settings.cache_enabled:
            try:
                client = get_redis()
                if client is None:
                    raise RuntimeError("Redis client unavailable")
                client.ping()
                deps["redis"] = DependencyStatus(status="ok")
            except Exception as exc:
                deps["redis"] = DependencyStatus(status="error", detail=str(exc))

        overall = "ok" if all(d.status == "ok" for d in deps.values()) else "degraded"
        return HealthResponse(
            status=overall,
            app=settings.app_name,
            environment=settings.app_env,
            dependencies=deps,
        )

    # ── /live  — liveness probe (process alive, no deep checks) ───────────────
    @app.get("/live", tags=["observability"], include_in_schema=False)
    def liveness() -> JSONResponse:
        """Kubernetes / Coolify liveness probe.

        Returns 200 as long as the process is running and the event loop is
        responsive.  Does NOT check database or Redis — use /ready for that.
        """
        return JSONResponse({"status": "alive", "app": settings.app_name})

    # ── /ready  — readiness probe (can the app serve traffic?) ───────────────
    @app.get("/ready", tags=["observability"], include_in_schema=False)
    def readiness() -> JSONResponse:
        """Kubernetes / Coolify readiness probe.

        Verifies that all required dependencies are reachable.
        Returns 200 if ready, 503 if any critical dependency is down.

        Checks
        ──────
        • PostgreSQL: SELECT 1
        • Redis: PING (only when cache_enabled=true)
        • Scheduler: running flag (only when scheduler_enabled=true)
        """
        checks: dict[str, str] = {}
        ready = True

        # PostgreSQL
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:
            checks["postgres"] = f"error: {exc}"
            ready = False

        # Redis
        if settings.cache_enabled:
            try:
                from cache.client import get_redis
                client = get_redis()
                if client is None:
                    raise RuntimeError("client unavailable")
                client.ping()
                checks["redis"] = "ok"
            except Exception as exc:
                checks["redis"] = f"error: {exc}"
                ready = False

        # Scheduler
        if settings.scheduler_enabled:
            scheduler = getattr(app.state, "scheduler", None)
            if scheduler is not None and scheduler.running:
                checks["scheduler"] = "ok"
            else:
                checks["scheduler"] = "not running"
                ready = False

        operational: dict[str, object] | None = None
        try:
            with SessionLocal() as db:
                operational = build_system_status(db)
            checks["operational"] = str(operational.get("status"))
            if operational.get("status") in ("NO-GO", "BLOCKED"):
                ready = False
        except Exception as exc:
            checks["operational"] = f"error: {exc}"
            ready = False

        status_code = 200 if ready else 503
        return JSONResponse(
            {
                "ready": ready,
                "checks": checks,
                "app": settings.app_name,
                "operational_status": operational.get("status") if operational else None,
                "decision": operational.get("decision") if operational else "NO-GO",
                "blockers": operational.get("blockers") if operational else ["operational_readiness_error"],  # noqa: E501
            },
            status_code=status_code,
        )

    # ── Routers ───────────────────────────────────────────────────────────────
    @app.get("/build-info", tags=["observability"], include_in_schema=False)
    def build_info() -> dict[str, str | None]:
        vcs_ref = os.getenv("VCS_REF") or os.getenv("SOURCE_COMMIT") or os.getenv("COMMIT_SHA")
        return {
            "revision": vcs_ref,
            "vcs_ref": vcs_ref,
            "build_timestamp": os.getenv("BUILD_TIMESTAMP"),
            "build_source": os.getenv("BUILD_SOURCE"),
            "source_state": os.getenv("SOURCE_STATE", "unknown"),
            "image_tag": os.getenv("IMAGE_TAG"),
            "coolify_resource_uuid": os.getenv("COOLIFY_RESOURCE_UUID"),
        }

    auth_dep = [Depends(verify_api_key)]
    app.include_router(system_status_router)
    app.include_router(api_router, dependencies=auth_dep)
    app.include_router(poupi_baby_router, dependencies=auth_dep)
    app.include_router(pipeline_router, dependencies=auth_dep)
    app.include_router(documentation_router, dependencies=auth_dep)
    app.include_router(data_quality_router, dependencies=auth_dep)
    app.include_router(alerts_router, dependencies=auth_dep)
    app.include_router(crypto_router, dependencies=auth_dep)
    app.include_router(edge_router, dependencies=auth_dep)
    app.include_router(real_estate_router, dependencies=auth_dep)
    app.include_router(sports_odds_router, dependencies=auth_dep)
    app.include_router(nba_router, dependencies=auth_dep)
    app.include_router(nba_quant_router, dependencies=auth_dep)
    app.include_router(scrapers_router, dependencies=auth_dep)
    app.include_router(runtime_router, dependencies=auth_dep)
    app.include_router(watchdog_router, dependencies=auth_dep)
    app.include_router(trading_validation_router, dependencies=auth_dep)
    # Operational Truth Layer — /health/operational, /health/runtime, etc.
    # No auth_dep: health endpoints are intentionally public (monitored by Prometheus/Grafana).
    app.include_router(operational_truth_router)
    # Operational Policy — /policy/operational (public, consumed by downstream services).
    app.include_router(operational_policy_router)
    # Adaptive Intelligence — /adaptive-intelligence/* (public, advisory-only).
    app.include_router(adaptive_intelligence_router)
    # Adaptive Policy Contract — /adaptive-policy/* (public, advisory-only).
    app.include_router(adaptive_policy_router)
    # Incident Event Bus — /api/v1/incidents/* (webhook receiver + query API).
    # Webhook endpoint é público (sem auth) para receber do Alertmanager.
    app.include_router(incident_bus_router)
    # Incident History — /api/v1/incidents/history/* (memória operacional + patterns).
    app.include_router(incident_history_router)
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    return app

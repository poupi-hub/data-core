"""Endpoint /api/v1/global-auto-health."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.global_auto_health.aggregator import run_global_auto_health

router = APIRouter(tags=["global-auto-health"])

_STATUS_HTTP: dict[str, int] = {
    "READY": 200,
    "DEGRADED": 200,
    "INVESTIGAR": 200,
    "BLOCKED": 503,
    "ARCHIVED": 200,
}


@router.get("/api/v1/global-auto-health", summary="Consolidated health across all active services")
def global_auto_health() -> JSONResponse:
    result = run_global_auto_health()
    http_code = _STATUS_HTTP.get(result["status"], 200)
    return JSONResponse(content=result, status_code=http_code)

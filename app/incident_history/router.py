"""
Incident History — API Router

Endpoints:
  GET  /api/v1/incidents/history/                    ← histórico recente
  GET  /api/v1/incidents/history/{alert_id}          ← histórico por alert_id
  POST /api/v1/incidents/history/                    ← registrar manualmente
  GET  /api/v1/incidents/history/patterns/           ← patterns agregados
  GET  /api/v1/incidents/history/patterns/{alert_id} ← pattern de um alerta específico
  GET  /api/v1/incidents/history/rca-hint/{alert_id} ← root cause sugerida (para RCA Engine)
  POST /api/v1/incidents/history/aggregate           ← trigger manual da agregação
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.incident_history.schemas import (
    AggregationResult,
    HistoryList,
    IncidentHistoryCreate,
    IncidentHistoryRead,
    IncidentPatternRead,
    PatternList,
)
from app.incident_history.service import IncidentHistoryService
from database.session import SessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/incidents/history", tags=["incident-history"])

_service = IncidentHistoryService()


def _get_db():
    with SessionLocal() as db:
        yield db


DbDep = Annotated[Session, Depends(_get_db)]


# ── History records ───────────────────────────────────────────────────────────

@router.get("/", response_model=HistoryList)
def list_history(
    db: DbDep,
    hours: int = Query(24, ge=1, le=720, description="Janela em horas"),
    service: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> HistoryList:
    """Lista incidentes resolvidos registrados no histórico."""
    items = _service.get_recent_history(db, hours=hours, service=service, limit=limit)
    return HistoryList(total=len(items), items=items)


@router.get("/alert/{alert_id}", response_model=HistoryList)
def history_for_alert(
    alert_id: str,
    db: DbDep,
    limit: int = Query(20, ge=1, le=100),
) -> HistoryList:
    """Histórico de ocorrências de um alerta específico."""
    items = _service.get_history_for_alert(db, alert_id=alert_id, limit=limit)
    return HistoryList(total=len(items), items=items)


@router.post("/", response_model=IncidentHistoryRead, status_code=201)
def record_history(body: IncidentHistoryCreate, db: DbDep) -> IncidentHistoryRead:
    """
    Registra manualmente um incidente resolvido.
    Usado pelo AI Agent (Fase 10) após completar a correção.
    """
    history = _service.record_manual(db, body)
    return IncidentHistoryRead.model_validate(history)


# ── Patterns ──────────────────────────────────────────────────────────────────

@router.get("/patterns/", response_model=PatternList)
def list_patterns(
    db: DbDep,
    service: str | None = Query(None),
    is_flapping: bool | None = Query(None, description="Filtrar alertas em flapping"),
    min_occurrences: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
) -> PatternList:
    """
    Lista padrões operacionais agregados por alert_id.
    Ordenado por total_occurrences desc.
    Usado pelo RCA Engine para priorizar alertas com histórico rico.
    """
    items = _service.get_all_patterns(
        db,
        service=service,
        is_flapping=is_flapping,
        min_occurrences=min_occurrences,
        limit=limit,
    )
    return PatternList(total=len(items), items=items)


@router.get("/patterns/{alert_id}", response_model=IncidentPatternRead)
def get_pattern(alert_id: str, db: DbDep) -> IncidentPatternRead:
    """Retorna o pattern agregado de um alert_id específico."""
    pattern = _service.get_pattern(db, alert_id)
    if not pattern:
        raise HTTPException(
            status_code=404,
            detail=f"No pattern found for alert_id={alert_id}. "
                   "Incident may not have occurred yet or aggregation hasn't run.",
        )
    return IncidentPatternRead.model_validate(pattern)


# ── RCA hint (para o RCA Engine — Fase 9) ────────────────────────────────────

@router.get("/rca-hint/{alert_id}")
def get_rca_hint(alert_id: str, db: DbDep) -> dict:
    """
    Retorna a root cause mais provável baseada no histórico.
    Usado pelo RCA Engine (Fase 9) como prior para o diagnóstico.

    Retorna 404 se histórico insuficiente (< 3 ocorrências resolvidas).
    """
    hint = _service.get_likely_root_cause(db, alert_id)
    if not hint:
        raise HTTPException(
            status_code=404,
            detail=f"Insufficient history for {alert_id} (need >= 3 resolved incidents with RCA).",
        )
    return hint


# ── Aggregation trigger ───────────────────────────────────────────────────────

@router.post("/aggregate", response_model=AggregationResult)
def trigger_aggregation(db: DbDep) -> AggregationResult:
    """
    Dispara manualmente o job de agregação de histórico.
    Normalmente chamado pelo APScheduler a cada hora.
    """
    result = _service.aggregate(db)
    logger.info("Manual aggregation triggered: %s", result)
    return result

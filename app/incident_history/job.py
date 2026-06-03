"""
Job APScheduler para agregação horária do Incident History.

Registrado pelo scheduler service durante o startup do data-core.
"""

import logging

from database.session import SessionLocal
from app.incident_history.service import IncidentHistoryService

logger = logging.getLogger(__name__)

_service = IncidentHistoryService()


def incident_history_aggregation_job() -> None:
    """
    Executa a cada hora — agrega IncidentEvents resolvidos em IncidentHistory
    e atualiza IncidentPatterns (MTTR, frequência, root causes).
    """
    logger.info("incident_history_aggregation_job starting")
    try:
        with SessionLocal() as db:
            result = _service.aggregate(db)
        logger.info(
            "incident_history_aggregation_job completed: "
            "%d events processed, %d history records, %d patterns updated, %d errors",
            result.processed_events,
            result.new_history_records,
            result.updated_patterns,
            result.errors,
        )
    except Exception:
        logger.exception("incident_history_aggregation_job failed")

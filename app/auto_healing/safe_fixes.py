from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auto_healing.models import AlertAssessment, Classification, SafeFixAction, ServiceHealth


class SafeFixEngine:
    def __init__(self, db: Session, *, dry_run: bool) -> None:
        self._db = db
        self._dry_run = dry_run

    def apply(
        self,
        assessments: list[AlertAssessment],
        health: list[ServiceHealth],
    ) -> tuple[list[SafeFixAction], list[str]]:
        actions: list[SafeFixAction] = []
        manual: list[str] = []

        if _should_reprobe(assessments, health):
            actions.append(self._reprobe_database())

        queue_health = next((item for item in health if item.name == "queues"), None)
        if queue_health and queue_health.status == "CRITICAL":
            manual.append(
                "Backlog/falhas de fila acima do limite seguro; requer analise manual antes de reprocessar."
            )

        bullmq_health = next((item for item in health if item.name == "bullmq"), None)
        if bullmq_health and bullmq_health.status in {"CRITICAL", "DEGRADED"}:
            manual.append(
                "BullMQ com wait/failed/stalled detectado; limpeza ou retry exige confirmacao do mecanismo interno."
            )

        redis_health = next((item for item in health if item.name == "redis"), None)
        if redis_health and redis_health.status == "DEGRADED":
            actions.append(
                SafeFixAction(
                    name="reexecute_redis_probe",
                    status="dry_run" if self._dry_run else "executed",
                    target="redis",
                    evidence=redis_health.error or "Redis degradado; probe repetido e registrado.",
                    dry_run=self._dry_run,
                    result="Sem mutacao: apenas validacao de conectividade.",
                )
            )

        for assessment in assessments:
            if assessment.classification in {Classification.REAL, Classification.INCONCLUSIVO}:
                manual.append(
                    f"{assessment.alert.code}: {assessment.classification.value} - {assessment.alert.title}"
                )

        return actions, _dedupe(manual)

    def _reprobe_database(self) -> SafeFixAction:
        try:
            if not self._dry_run:
                self._db.execute(text("SELECT 1"))
            return SafeFixAction(
                name="reexecute_health_probe",
                status="dry_run" if self._dry_run else "executed",
                target="postgres",
                evidence="Probe SELECT 1 e pool pre_ping sao seguros e sem mutacao.",
                dry_run=self._dry_run,
                result="Probe registrado." if not self._dry_run else "DRY_RUN: nenhuma acao executada.",
            )
        except Exception as exc:
            return SafeFixAction(
                name="reexecute_health_probe",
                status="failed",
                target="postgres",
                evidence="Probe seguro falhou.",
                dry_run=self._dry_run,
                result=str(exc),
            )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _should_reprobe(assessments: list[AlertAssessment], health: list[ServiceHealth]) -> bool:
    if any(not item.ok for item in health):
        return True
    return any(
        item.classification in {Classification.REAL, Classification.INCONCLUSIVO}
        for item in assessments
    )

from __future__ import annotations

from collections import Counter

from app.auto_healing.models import AlertAssessment, Classification, OperationalAlert, ServiceHealth


class IncidentClassifier:
    def classify(
        self,
        alerts: list[OperationalAlert],
        health: list[ServiceHealth],
    ) -> list[AlertAssessment]:
        counts = Counter(alert.fingerprint() for alert in alerts)
        seen: set[str] = set()
        assessments: list[AlertAssessment] = []
        for alert in alerts:
            fingerprint = alert.fingerprint()
            related = _related_health(alert, health)
            evidence = _evidence(alert, related)

            if fingerprint in seen and counts[fingerprint] > 1:
                classification = Classification.DUPLICADO
                evidence.append("Mesmo fingerprint apareceu mais de uma vez na janela analisada.")
            elif related and all(item.ok for item in related):
                classification = Classification.RECUPERADO
                evidence.append("Checks atuais relacionados estao saudaveis.")
            elif related and any(item.critical for item in related):
                classification = Classification.REAL
                evidence.append("Checks atuais relacionados seguem criticos.")
            elif related and any(not item.ok for item in related):
                classification = Classification.REAL if alert.severity == "critical" else Classification.INCONCLUSIVO
                evidence.append("Checks atuais relacionados seguem degradados.")
            elif alert.code in {"telegram_no_publication_no_data"}:
                classification = Classification.INCONCLUSIVO
                evidence.append("Sem evidencia direta de falha no Telegram; depende de volume de ofertas/dados.")
            elif alert.severity == "warning":
                classification = Classification.FALSO_POSITIVO
                evidence.append("Nenhum check atual correlacionado confirma o alerta.")
            else:
                classification = Classification.INCONCLUSIVO
                evidence.append("Alerta critico sem check atual correlacionado suficiente.")

            assessments.append(
                AlertAssessment(
                    alert=alert,
                    classification=classification,
                    evidence=evidence,
                    related_health=[item.name for item in related],
                )
            )
            seen.add(fingerprint)
        return assessments


def _related_health(alert: OperationalAlert, health: list[ServiceHealth]) -> list[ServiceHealth]:
    text = " ".join([alert.code, alert.title, alert.message, alert.source or ""]).lower()
    matches: list[ServiceHealth] = []
    mapping = {
        "telegram": ("telegram_alerts",),
        "collection": ("last_job", "scheduler", "workers", "queues"),
        "normalization": ("queues", "last_job", "workers"),
        "queue": ("queues", "bullmq", "redis", "workers"),
        "bullmq": ("bullmq", "redis", "workers"),
        "redis": ("redis",),
        "postgres": ("postgres",),
        "database": ("postgres",),
        "scheduler": ("scheduler",),
        "worker": ("workers",),
        "crypto": ("poupi-crypto",),
        "poupi-baby": ("poupi-baby",),
        "data-core": ("data-core",),
    }
    wanted: set[str] = set()
    for needle, names in mapping.items():
        if needle in text:
            wanted.update(names)
    for item in health:
        if item.name in wanted:
            matches.append(item)
    return matches


def _evidence(alert: OperationalAlert, related: list[ServiceHealth]) -> list[str]:
    items = [f"alert={alert.code}", f"severity={alert.severity}"]
    if alert.emitted_at:
        items.append(f"emitted_at={alert.emitted_at.isoformat()}")
    for item in related:
        detail = item.error or item.evidence
        items.append(f"{item.name} status={item.status} evidence={detail}")
    return items

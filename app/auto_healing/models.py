from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Classification(str, Enum):
    REAL = "REAL"
    FALSO_POSITIVO = "FALSO_POSITIVO"
    DUPLICADO = "DUPLICADO"
    RECUPERADO = "RECUPERADO"
    INCONCLUSIVO = "INCONCLUSIVO"


class GeneralStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass
class OperationalAlert:
    code: str
    title: str
    message: str
    severity: str
    source: str | None = None
    emitted_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        return "|".join([self.code, self.source or "", self.title])

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["emitted_at"] = self.emitted_at.isoformat() if self.emitted_at else None
        return payload


@dataclass
class ServiceHealth:
    name: str
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status.upper() in {"OK", "READY", "HEALTHY", "ALIVE"}

    @property
    def critical(self) -> bool:
        return self.status.upper() in {"CRITICAL", "DOWN", "NO-GO", "ERROR"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AlertAssessment:
    alert: OperationalAlert
    classification: Classification
    evidence: list[str] = field(default_factory=list)
    related_health: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert": self.alert.to_dict(),
            "classification": self.classification.value,
            "evidence": self.evidence,
            "related_health": self.related_health,
        }


@dataclass
class SafeFixAction:
    name: str
    status: str
    target: str
    evidence: str
    dry_run: bool = True
    result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WatchdogExecution:
    timestamp: datetime
    status: GeneralStatus
    dry_run: bool
    alerts_analyzed: list[AlertAssessment]
    service_health: list[ServiceHealth]
    actions: list[SafeFixAction]
    manual_pending: list[str]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "dry_run": self.dry_run,
            "alerts_analyzed": [item.to_dict() for item in self.alerts_analyzed],
            "service_health": [item.to_dict() for item in self.service_health],
            "actions": [item.to_dict() for item in self.actions],
            "manual_pending": self.manual_pending,
            "errors": self.errors,
        }


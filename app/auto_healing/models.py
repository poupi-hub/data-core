from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class HealOutcome(str, Enum):
    RECOVERED = "RECOVERED"  # healed automatically, no notification needed
    FAILED = "FAILED"        # healing attempted but failed, notify
    SKIPPED = "SKIPPED"      # no healer available and service is critical, notify


class Classification(str, Enum):
    HEALTHY = "HEALTHY"
    OBSERVATION = "OBSERVATION"
    DEGRADED = "DEGRADED"
    AUTO_HEALABLE_DRY_RUN = "AUTO_HEALABLE_DRY_RUN"
    ALERT_ONLY = "ALERT_ONLY"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    BLOCKED_BY_SAFETY = "BLOCKED_BY_SAFETY"


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
class AutoHealingEvent:
    id: str
    incident_id: str
    correlation_id: str
    failure_fingerprint: str
    created_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    environment: str
    service: str
    component: str
    probe_name: str
    signal_name: str
    severity: str
    classification: Classification
    confidence_score: float
    evidence_count: int
    evidence_sources: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)
    potential_false_positive: bool = False
    dry_run: bool = True
    decision: str = "record_decision"
    recommended_action: str | None = None
    action_allowed: bool = False
    safety_reason: str = "Phase 2 is dry-run only"
    attempt_number: int = 0
    cooldown_until: datetime | None = None
    source: str = "auto_healing"
    status: str = "recorded"
    telegram_should_alert: bool = False
    telegram_reason: str = "Phase 2 does not send Telegram"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("created_at", "first_seen_at", "last_seen_at", "cooldown_until"):
            value = payload.get(key)
            payload[key] = value.isoformat() if value else None
        payload["classification"] = self.classification.value
        payload["dry_run"] = True
        payload["action_allowed"] = False
        payload["telegram_should_alert"] = False
        return payload


@dataclass
class HealResult:
    service: str
    outcome: HealOutcome
    detail: str
    rows_affected: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "rows_affected": self.rows_affected,
            "error": self.error,
        }


@dataclass
class WatchdogExecution:
    timestamp: datetime
    status: GeneralStatus
    dry_run: bool
    events: list[AutoHealingEvent]
    service_health: list[ServiceHealth]
    heal_results: list[HealResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "dry_run": True,
            "events": [item.to_dict() for item in self.events],
            "service_health": [item.to_dict() for item in self.service_health],
            "heal_results": [item.to_dict() for item in self.heal_results],
            "errors": self.errors,
        }

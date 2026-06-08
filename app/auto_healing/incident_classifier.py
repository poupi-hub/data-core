from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auto_healing.models import (
    AutoHealingEvent,
    Classification,
    OperationalAlert,
    ServiceHealth,
)

AUTO_HEALABLE_MIN_CONFIDENCE = 0.75
AUTO_HEALABLE_MIN_EVIDENCE = 2

COOLDOWNS = {
    Classification.OBSERVATION: timedelta(minutes=15),
    Classification.DEGRADED: timedelta(minutes=30),
    Classification.AUTO_HEALABLE_DRY_RUN: timedelta(minutes=60),
    Classification.ALERT_ONLY: timedelta(minutes=60),
    Classification.MANUAL_REQUIRED: timedelta(hours=2),
    Classification.BLOCKED_BY_SAFETY: timedelta(hours=2),
}

SOURCE_WEIGHTS = {
    "endpoint": 0.35,
    "prometheus": 0.30,
    "heartbeat": 0.35,
    "queue": 0.30,
    "docker": 0.30,
    "logs": 0.10,
    "backup": 0.90,
    "redis": 0.35,
}


class IncidentClassifier:
    def classify(
        self,
        alerts: list[OperationalAlert],
        health: list[ServiceHealth],
        *,
        now: datetime | None = None,
        previous_events: list[AutoHealingEvent] | None = None,
    ) -> list[AutoHealingEvent]:
        timestamp = now or datetime.now(timezone.utc)
        previous_events = previous_events or []
        candidates = [_candidate_from_health(item, timestamp) for item in health]
        candidates.extend(_candidate_from_alert(item, timestamp) for item in alerts)

        events: list[AutoHealingEvent] = []
        for candidate in candidates:
            if candidate["healthy"]:
                continue
            event = _event_from_candidate(candidate, timestamp, previous_events)
            events.append(event)
        return events


def _candidate_from_health(item: ServiceHealth, timestamp: datetime) -> dict[str, Any]:
    service, component = _service_component(item.name)
    source, weight = _source_weight(item)
    signal = _signal_name(item)
    fingerprint = _failure_fingerprint(item)
    evidence_sources = _dedupe_sources([source, *_extra_sources(item)])
    confidence = _confidence(evidence_sources, {source: weight})
    duration_or_trend = _has_duration_or_trend(item.evidence)
    healthy = item.ok
    return {
        "id_seed": f"{timestamp.isoformat()}:{service}:{component}:{signal}:{fingerprint}",
        "service": service,
        "component": component,
        "probe_name": _probe_name(item.name),
        "signal_name": signal,
        "severity": "critical" if item.critical else "warning",
        "failure_fingerprint": fingerprint,
        "evidence_sources": evidence_sources,
        "confidence_score": confidence,
        "evidence": _safe_evidence(item),
        "source": source,
        "healthy": healthy,
        "duration_or_trend": duration_or_trend,
        "recommended_action": _recommendation_for(item.name),
    }


def _candidate_from_alert(alert: OperationalAlert, timestamp: datetime) -> dict[str, Any]:
    source = _alert_source(alert)
    fingerprint = _sanitize_fingerprint(alert.code)
    confidence = SOURCE_WEIGHTS[source]
    service, component = _alert_service_component(alert)
    return {
        "id_seed": f"{timestamp.isoformat()}:{service}:{component}:{alert.code}:{fingerprint}",
        "service": service,
        "component": component,
        "probe_name": "alert_history_probe",
        "signal_name": _enum_token(alert.code),
        "severity": "critical" if alert.severity == "critical" else "warning",
        "failure_fingerprint": fingerprint,
        "evidence_sources": [source],
        "confidence_score": confidence,
        "evidence": {
            "alert_code": alert.code,
            "title": alert.title,
            "source": alert.source,
            "emitted_at": alert.emitted_at.isoformat() if alert.emitted_at else None,
        },
        "source": source,
        "healthy": False,
        "duration_or_trend": source == "backup",
        "recommended_action": _alert_recommendation(alert),
    }


def _event_from_candidate(
    candidate: dict[str, Any],
    timestamp: datetime,
    previous_events: list[AutoHealingEvent],
) -> AutoHealingEvent:
    incident_id = _incident_id(candidate)
    related_previous = [item for item in previous_events if item.incident_id == incident_id]
    first_seen_at = min((item.first_seen_at for item in related_previous), default=timestamp)
    evidence_sources = candidate["evidence_sources"]
    evidence_count = len(evidence_sources)
    confidence = min(1.0, round(float(candidate["confidence_score"]), 2))
    flapping = _is_flapping(related_previous)
    potential_false_positive = flapping or evidence_count == 1
    classification = _classification(
        candidate,
        evidence_count,
        confidence,
        potential_false_positive,
    )
    status = (
        "suppressed"
        if _cooldown_active(classification, timestamp, related_previous)
        else "recorded"
    )
    if status == "suppressed":
        potential_false_positive = True

    decision = (
        "recommend_action"
        if classification == Classification.AUTO_HEALABLE_DRY_RUN
        else "record_decision"
    )
    recommended_action = candidate["recommended_action"] if decision == "recommend_action" else None
    cooldown_until = timestamp + COOLDOWNS.get(classification, timedelta(minutes=15))
    event_id = _stable_id(candidate["id_seed"])
    correlation_id = (
        f"auto-healer-{timestamp:%Y%m%dT%H%M%SZ}-"
        f"{candidate['service']}-{candidate['component']}"
    )
    return AutoHealingEvent(
        id=event_id,
        incident_id=incident_id,
        correlation_id=correlation_id,
        failure_fingerprint=candidate["failure_fingerprint"],
        created_at=timestamp,
        first_seen_at=first_seen_at,
        last_seen_at=timestamp,
        environment="production",
        service=candidate["service"],
        component=candidate["component"],
        probe_name=candidate["probe_name"],
        signal_name=candidate["signal_name"],
        severity=candidate["severity"],
        classification=classification,
        confidence_score=confidence,
        evidence_count=evidence_count,
        evidence_sources=evidence_sources,
        evidence=candidate["evidence"],
        potential_false_positive=potential_false_positive,
        decision=decision,
        recommended_action=recommended_action,
        cooldown_until=cooldown_until,
        source=candidate["source"],
        status=status,
    )


def _classification(
    candidate: dict[str, Any],
    evidence_count: int,
    confidence: float,
    potential_false_positive: bool,
) -> Classification:
    if potential_false_positive and evidence_count == 1:
        if candidate["source"] == "backup" and confidence >= 0.80:
            return Classification.MANUAL_REQUIRED
        return Classification.OBSERVATION
    if candidate["component"] in {"queue", "readiness"} and not candidate["duration_or_trend"]:
        return Classification.OBSERVATION
    if confidence >= AUTO_HEALABLE_MIN_CONFIDENCE and evidence_count >= AUTO_HEALABLE_MIN_EVIDENCE:
        return Classification.AUTO_HEALABLE_DRY_RUN
    if confidence >= 0.80 and candidate["source"] == "backup":
        return Classification.MANUAL_REQUIRED
    if confidence >= 0.70 and evidence_count >= 2:
        return Classification.ALERT_ONLY
    if confidence >= 0.50 and evidence_count >= 2:
        return Classification.DEGRADED
    return Classification.OBSERVATION


def _cooldown_active(
    classification: Classification,
    timestamp: datetime,
    previous_events: list[AutoHealingEvent],
) -> bool:
    if not previous_events:
        return False
    cooldown = COOLDOWNS.get(classification, timedelta(minutes=15))
    last = max(item.last_seen_at for item in previous_events)
    return timestamp - last < cooldown


def _is_flapping(previous_events: list[AutoHealingEvent]) -> bool:
    if len(previous_events) < 3:
        return False
    recent = previous_events[-3:]
    return len({item.classification for item in recent}) > 1


def _service_component(name: str) -> tuple[str, str]:
    mapping = {
        "postgres": ("shared-infra", "postgres"),
        "redis": ("shared-infra", "redis"),
        "bullmq": ("poupi-baby", "queue"),
        "queues": ("data-core", "queue"),
        "scheduler": ("data-core", "scheduler"),
        "workers": ("data-core", "worker"),
        "last_job": ("data-core", "pipeline"),
        "telegram_alerts": ("alertmanager", "telegram"),
        "data-core": ("data-core", "api"),
        "poupi-crypto": ("poupi-crypto", "api"),
        "poupi-baby": ("poupi-baby", "api"),
    }
    return mapping.get(name, (name, "service"))


def _source_weight(item: ServiceHealth) -> tuple[str, float]:
    if item.name in {"data-core", "poupi-crypto", "poupi-baby"}:
        return "endpoint", SOURCE_WEIGHTS["endpoint"]
    if item.name in {"scheduler", "workers", "last_job"}:
        return "heartbeat", SOURCE_WEIGHTS["heartbeat"]
    if item.name in {"bullmq", "queues"}:
        return "queue", SOURCE_WEIGHTS["queue"]
    if item.name == "redis":
        return "redis", SOURCE_WEIGHTS["redis"]
    if item.name == "telegram_alerts":
        return "logs", SOURCE_WEIGHTS["logs"]
    return "logs", SOURCE_WEIGHTS["logs"]


def _extra_sources(item: ServiceHealth) -> list[str]:
    raw = item.evidence.get("evidence_sources")
    if isinstance(raw, list):
        return [str(value) for value in raw]
    if item.evidence.get("prometheus_target_down"):
        return ["prometheus"]
    if item.evidence.get("docker_unhealthy"):
        return ["docker"]
    if item.evidence.get("redis_ping") in {"failed", False}:
        return ["redis"]
    return []


def _confidence(sources: list[str], overrides: dict[str, float]) -> float:
    score = 0.0
    for source in sources:
        score += overrides.get(source, SOURCE_WEIGHTS.get(source, 0.10))
    return min(1.0, round(score, 2))


def _dedupe_sources(sources: list[str]) -> list[str]:
    redundant_groups = {
        "endpoint": "service_reachability",
        "prometheus": "service_reachability",
        "docker": "service_reachability",
    }
    seen_groups: set[str] = set()
    result: list[str] = []
    for source in sources:
        group = redundant_groups.get(source, source)
        if group in seen_groups:
            continue
        seen_groups.add(group)
        result.append(source)
    return result


def _signal_name(item: ServiceHealth) -> str:
    if item.name == "scheduler":
        return "SCHEDULER_HEARTBEAT_STALE"
    if item.name == "workers":
        return "WORKER_HEARTBEAT_STALE"
    if item.name in {"bullmq", "queues"}:
        return "QUEUE_BACKLOG_HIGH"
    if item.name == "redis":
        return "REDIS_UNREACHABLE"
    if item.name == "postgres":
        return "POSTGRES_UNAVAILABLE"
    if item.name == "telegram_alerts":
        return "TELEGRAM_DELIVERY_FAILED"
    if item.name in {"data-core", "poupi-crypto", "poupi-baby"}:
        return "ENDPOINT_HEALTH_FAILED"
    return "SERVICE_DEGRADED"


def _probe_name(name: str) -> str:
    if name in {"data-core", "poupi-crypto", "poupi-baby"}:
        return "http_health_probe"
    if name in {"scheduler", "workers"}:
        return f"{name.rstrip('s')}_heartbeat_probe"
    if name in {"bullmq", "queues"}:
        return "queue_depth_probe"
    if name == "redis":
        return "redis_ping_probe"
    if name == "postgres":
        return "postgres_ready_probe"
    if name == "telegram_alerts":
        return "telegram_delivery_probe"
    return "service_health_probe"


def _failure_fingerprint(item: ServiceHealth) -> str:
    if item.error:
        return _sanitize_fingerprint(item.error)
    if item.name in {"bullmq", "queues"}:
        evidence = item.evidence
        if _has_duration_or_trend(evidence):
            return "queue_duration_or_trend"
        return "queue_single_sample"
    if item.name in {"scheduler", "workers"}:
        return "heartbeat_stale_or_missing"
    return _sanitize_fingerprint(item.status)


def _safe_evidence(item: ServiceHealth) -> dict[str, Any]:
    evidence = dict(item.evidence)
    evidence.pop("url", None)
    if item.error:
        evidence["error_hint"] = _sanitize_fingerprint(item.error)
    evidence["status"] = item.status
    return evidence


def _has_duration_or_trend(evidence: dict[str, Any]) -> bool:
    return bool(
        evidence.get("trend_minutes", 0) >= 10
        or evidence.get("probe_cycles", 0) >= 2
        or evidence.get("duration_seconds", 0) >= 120
        or evidence.get("growing") is True
    )


def _recommendation_for(name: str) -> str | None:
    mapping = {
        "scheduler": "diagnose_scheduler_heartbeat",
        "workers": "diagnose_worker_heartbeat",
        "bullmq": "diagnose_worker_redis_and_downstream",
        "queues": "diagnose_pipeline_backlog",
        "redis": "diagnose_redis_connectivity",
        "data-core": "diagnose_api_health",
        "poupi-crypto": "diagnose_api_health",
        "poupi-baby": "diagnose_api_health",
    }
    return mapping.get(name)


def _alert_source(alert: OperationalAlert) -> str:
    text = " ".join([alert.code, alert.title, alert.message, alert.source or ""]).lower()
    if "backup" in text or "restore" in text:
        return "backup"
    if "telegram" in text:
        return "logs"
    return "logs"


def _alert_service_component(alert: OperationalAlert) -> tuple[str, str]:
    text = " ".join([alert.code, alert.source or ""]).lower()
    if "backup" in text or "restore" in text:
        return "backups", alert.code
    if "telegram" in text:
        return "alertmanager", "telegram"
    return "data-core", "alert"


def _alert_recommendation(alert: OperationalAlert) -> str | None:
    source = _alert_source(alert)
    if source == "backup":
        return "human_review_backup_logs"
    return None


def _incident_id(candidate: dict[str, Any]) -> str:
    key = ":".join(
        [
            candidate["service"],
            candidate["component"],
            candidate["signal_name"],
            candidate["failure_fingerprint"],
        ]
    )
    return f"inc-{_stable_id(key)[:16]}"


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sanitize_fingerprint(value: object) -> str:
    token = _enum_token(str(value))[:80]
    return token or "unknown"


def _enum_token(value: str) -> str:
    chars = []
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "_":
            chars.append("_")
    return "".join(chars).strip("_").upper()

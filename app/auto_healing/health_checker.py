from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.auto_healing.models import ServiceHealth
from app.pipeline.models import PipelineRun
from app.raw.models import RawCollection
from app.runtime.heartbeat import read_worker_heartbeat
from app.runtime.scheduler_heartbeat import heartbeat_age_seconds, read_scheduler_heartbeat
from app.watchdog.models import TelegramPublicationEvent
from core.config import settings


class HealthChecker:
    def __init__(self, db: Session) -> None:
        self._db = db

    def run(self) -> list[ServiceHealth]:
        checks = [
            self._postgres(),
            self._redis(),
            self._bullmq_queues(),
            self._scheduler_heartbeat(),
            self._worker_heartbeat(),
            self._last_pipeline_job(),
            self._telegram_alert_persistence(),
            self._normalization_backlog(),
        ]
        checks.extend(self._service_endpoints())
        return checks

    def _postgres(self) -> ServiceHealth:
        try:
            self._db.execute(text("SELECT 1"))
            return ServiceHealth("postgres", "OK", {"query": "SELECT 1"})
        except Exception as exc:
            return ServiceHealth("postgres", "CRITICAL", error=str(exc))

    def _redis(self) -> ServiceHealth:
        try:
            import redis as redis_lib

            client = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2, decode_responses=True)
            pong = client.ping()
            info: dict[str, Any] = {"ping": bool(pong), "url": _redact_url(settings.redis_url)}
            try:
                info["dbsize"] = client.dbsize()
            except Exception:
                pass
            return ServiceHealth("redis", "OK", info)
        except Exception as exc:
            status = "CRITICAL" if settings.cache_enabled else "DEGRADED"
            return ServiceHealth("redis", status, {"required": settings.cache_enabled}, str(exc))

    def _bullmq_queues(self) -> ServiceHealth:
        try:
            import redis as redis_lib

            client = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2, decode_responses=True)
            counts: dict[str, int] = {}
            for key in client.scan_iter(match="bull:*", count=200):
                suffix = str(key).rsplit(":", 1)[-1]
                if suffix not in {"wait", "delayed", "failed", "stalled"}:
                    continue
                try:
                    key_type = client.type(key)
                    if key_type == "list":
                        size = client.llen(key)
                    elif key_type == "zset":
                        size = client.zcard(key)
                    elif key_type == "set":
                        size = client.scard(key)
                    else:
                        continue
                    counts[suffix] = counts.get(suffix, 0) + int(size)
                except Exception:
                    continue
            failed = counts.get("failed", 0)
            stalled = counts.get("stalled", 0)
            waiting = counts.get("wait", 0) + counts.get("delayed", 0)
            status = "CRITICAL" if stalled > 0 or failed > 100 else ("DEGRADED" if waiting > 500 or failed > 0 else "OK")
            return ServiceHealth("bullmq", status, {"counts": counts})
        except Exception as exc:
            return ServiceHealth("bullmq", "DEGRADED", error=str(exc))

    def _scheduler_heartbeat(self) -> ServiceHealth:
        heartbeat = read_scheduler_heartbeat()
        age = heartbeat_age_seconds()
        if heartbeat is None:
            return ServiceHealth("scheduler", "DEGRADED", {"heartbeat": "missing"})
        status = "OK" if age is not None and age <= 15 * 60 else "DEGRADED"
        return ServiceHealth(
            "scheduler",
            status,
            {
                "heartbeat_age_seconds": age,
                "last_job": heartbeat.get("last_job"),
                "last_job_status": heartbeat.get("last_job_status"),
                "consecutive_failures": heartbeat.get("consecutive_failures"),
            },
        )

    def _worker_heartbeat(self) -> ServiceHealth:
        heartbeat = read_worker_heartbeat()
        if not heartbeat:
            return ServiceHealth("workers", "DEGRADED", {"heartbeat": "missing"})
        age = _age_from_epoch(heartbeat.get("timestamp_epoch"))
        status = "OK" if age is not None and age <= max(settings.worker_pipeline_interval_seconds * 2, 180) else "DEGRADED"
        return ServiceHealth(
            "workers",
            status,
            {"heartbeat_age_seconds": age, "worker_status": heartbeat.get("status")},
        )

    def _last_pipeline_job(self) -> ServiceHealth:
        try:
            latest = self._db.query(func.max(PipelineRun.finished_at)).scalar()
            age = _age_from_datetime(latest)
            status = "OK" if age is not None and age <= 6 * 3600 else "DEGRADED"
            return ServiceHealth("last_job", status, {"last_finished_at": _iso(latest), "age_seconds": age})
        except Exception as exc:
            return ServiceHealth("last_job", "DEGRADED", error=str(exc))

    def _telegram_alert_persistence(self) -> ServiceHealth:
        try:
            total = self._db.query(func.count(TelegramPublicationEvent.id)).scalar() or 0
            last = self._db.query(func.max(TelegramPublicationEvent.published_at)).scalar()
            return ServiceHealth(
                "telegram_alerts",
                "OK",
                {"publication_events_total": total, "last_publication_event_at": _iso(last)},
            )
        except Exception as exc:
            return ServiceHealth("telegram_alerts", "DEGRADED", error=str(exc))

    def _normalization_backlog(self) -> ServiceHealth:
        try:
            pending = (
                self._db.query(func.count(RawCollection.id))
                .filter(RawCollection.processing_status == "normalization_pending")
                .scalar()
                or 0
            )
            failed = (
                self._db.query(func.count(RawCollection.id))
                .filter(RawCollection.processing_status == "normalization_failed")
                .scalar()
                or 0
            )
            if pending > 1000 or failed > 500:
                status = "CRITICAL"
            elif pending > 250 or failed > 100:
                status = "DEGRADED"
            else:
                status = "OK"
            return ServiceHealth("queues", status, {"pending_normalization": pending, "failed_normalization": failed})
        except Exception as exc:
            return ServiceHealth("queues", "DEGRADED", error=str(exc))

    def _service_endpoints(self) -> list[ServiceHealth]:
        services = _configured_services()
        results: list[ServiceHealth] = []
        for name, base_url in services.items():
            results.append(_probe_service(name, base_url))
        return results


def _configured_services() -> dict[str, str]:
    services = {
        "data-core": os.getenv("DATA_CORE_INTERNAL_URL", f"http://localhost:{settings.api_port}"),
        "poupi-crypto": settings.poupi_crypto_internal_url,
    }
    if settings.poupi_baby_url:
        services["poupi-baby"] = settings.poupi_baby_url
    for item in settings.auto_healing_service_urls.split(","):
        if not item.strip() or "=" not in item:
            continue
        name, url = item.split("=", 1)
        services[name.strip()] = url.strip()
    return {name: url.rstrip("/") for name, url in services.items() if url}


def _probe_service(name: str, base_url: str) -> ServiceHealth:
    url = f"{base_url}/health"
    try:
        response = httpx.get(url, timeout=4.0)
        status = "OK" if 200 <= response.status_code < 300 else "DEGRADED"
        evidence: dict[str, Any] = {"url": url, "status_code": response.status_code}
        try:
            body = response.json()
            evidence["response_status"] = body.get("status") or body.get("ready") or body.get("app")
        except Exception:
            evidence["body_preview"] = response.text[:120]
        return ServiceHealth(name, status, evidence)
    except Exception as exc:
        return ServiceHealth(name, "DEGRADED", {"url": url}, str(exc))


def _age_from_epoch(value: object) -> int | None:
    try:
        return max(0, int(datetime.now(timezone.utc).timestamp() - float(value)))
    except Exception:
        return None


def _age_from_datetime(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - value).total_seconds()))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _redact_url(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    host = rest.split("@", 1)[1]
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"

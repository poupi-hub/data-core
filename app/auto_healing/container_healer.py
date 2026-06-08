"""Container restart healers for RedisDown, SchedulerStopped, WorkerStopped.

Safety constraints (hard-coded, not config-driven):
- APIDown: NO HEALER — restarting the API risks write corruption mid-request.
- PostgresDown: NO HEALER — never restart the database automatically.
- Redis: restart is safe; Redis is stateless for our use-case (cache + BullMQ).
- Scheduler: restart is safe; it is a stateless cron-like process.
- Worker: restart is safe; worker uses idempotent pipeline stages.

Requires /var/run/docker.sock mounted read-only in the scheduler container.
Falls back gracefully if Docker SDK is unavailable or socket is not mounted.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.auto_healing.models import HealOutcome, HealResult, ServiceHealth

logger = logging.getLogger(__name__)

# Env var that carries the Coolify project UUID — used to scope container lookups.
_PROJECT_UUID_ENV = "COOLIFY_RESOURCE_UUID"
# Redis container is in a separate compose project; use explicit name.
_REDIS_CONTAINER_ENV = "REDIS_CONTAINER_NAME"
_REDIS_CONTAINER_DEFAULT = "multi_project_infra-redis-1"

# Maximum seconds to wait for a container to come back healthy after restart.
_RESTART_WAIT_SECONDS = 30


@runtime_checkable
class ContainerHealer(Protocol):
    name: str
    target_service: str  # matches ServiceHealth.name

    def can_heal(self, health: ServiceHealth) -> bool: ...
    def heal(self, db: Session) -> HealResult: ...


def _docker_client():
    """Return a Docker SDK client connected to the local socket."""
    import docker  # noqa: PLC0415 — lazy import; docker optional dep
    return docker.from_env()


def _find_compose_container(service_name: str) -> str | None:
    """Find a running container for the given compose service in this project."""
    try:
        client = _docker_client()
        project = os.getenv(_PROJECT_UUID_ENV, "")
        if not project:
            logger.warning("container_healer: %s env not set", _PROJECT_UUID_ENV)
            return None
        containers = client.containers.list(all=True, filters={
            "label": [
                f"com.docker.compose.project={project}",
                f"com.docker.compose.service={service_name}",
            ]
        })
        if not containers:
            logger.warning(
                "container_healer: no container found for service=%s project=%s",
                service_name, project,
            )
            return None
        return containers[0].name
    except Exception as exc:
        logger.warning("container_healer: _find_compose_container failed: %s", exc)
        return None


def _restart_container(container_name: str, service: str) -> HealResult:
    """Issue docker restart and return HealResult."""
    try:
        client = _docker_client()
        container = client.containers.get(container_name)
        logger.info("container_healer: restarting %s", container_name)
        container.restart(timeout=10)
        logger.info("container_healer: restarted %s successfully", container_name)
        return HealResult(
            service=service,
            outcome=HealOutcome.RECOVERED,
            detail=f"Container {container_name} restarted successfully",
        )
    except Exception as exc:
        logger.error("container_healer: failed to restart %s: %s", container_name, exc)
        return HealResult(
            service=service,
            outcome=HealOutcome.FAILED,
            detail=f"docker restart {container_name} raised an exception",
            error=str(exc),
        )


class RedisRestartHealer:
    """Restart the Redis container when Redis is unreachable.

    Prohibited: restart is safe — Redis is a cache layer with no write-ahead log
    that needs protecting. BullMQ state in Redis is ephemeral by design.
    """

    name = "restart_redis"
    target_service = "redis"

    def can_heal(self, health: ServiceHealth) -> bool:
        return health.name == "redis" and not health.ok

    def heal(self, db: Session) -> HealResult:  # noqa: ARG002
        container_name = os.getenv(_REDIS_CONTAINER_ENV, _REDIS_CONTAINER_DEFAULT)
        return _restart_container(container_name, "redis")


class SchedulerRestartHealer:
    """Restart the scheduler container when its heartbeat is stale."""

    name = "restart_scheduler"
    target_service = "scheduler"

    def can_heal(self, health: ServiceHealth) -> bool:
        return health.name == "scheduler" and not health.ok

    def heal(self, db: Session) -> HealResult:  # noqa: ARG002
        container_name = _find_compose_container("scheduler")
        if container_name is None:
            return HealResult(
                service="scheduler",
                outcome=HealOutcome.FAILED,
                detail="could not locate scheduler container via Docker API",
                error="container not found",
            )
        return _restart_container(container_name, "scheduler")


class WorkerRestartHealer:
    """Restart the worker container when its heartbeat is stale."""

    name = "restart_worker"
    target_service = "workers"

    def can_heal(self, health: ServiceHealth) -> bool:
        return health.name == "workers" and not health.ok

    def heal(self, db: Session) -> HealResult:  # noqa: ARG002
        container_name = _find_compose_container("worker")
        if container_name is None:
            return HealResult(
                service="workers",
                outcome=HealOutcome.FAILED,
                detail="could not locate worker container via Docker API",
                error="container not found",
            )
        return _restart_container(container_name, "workers")

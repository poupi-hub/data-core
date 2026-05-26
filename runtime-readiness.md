# Runtime Readiness

This document defines the operational readiness contract for data-core.

## Endpoint contract

- `/health`: simple API/runtime liveness.
- `/ready`: operational gate. It must return non-200 unless `/system-status.status == READY`.
- `/system-status`: full operational truth.
- `/health/business`: compact operational summary.
- `/metrics`: Prometheus metrics, including DB-backed pipeline truth gauges.

## Status classes

- `READY`: all critical operational gates pass.
- `DEGRADED`: partial operation exists, but at least one non-critical subsystem is degraded.
- `BLOCKED`: worker absent, normalization stopped, analytics stopped, backlog critical or scheduler critical.
- `NO-GO`: infrastructure failure or production readiness cannot be defended with evidence.

## READY gates

`READY` requires:

- API process reachable.
- Postgres reachable and queryable.
- Redis reachable only if `CACHE_ENABLED=true`.
- Dedicated worker active with fresh heartbeat.
- Scheduler healthy with fresh watchdog heartbeat.
- No real scheduler restart loop.
- No critical raw backlog.
- Crypto normalization recent.
- Crypto analytics recent.
- `collection-readiness.ready=true`.
- At least one ecommerce provider healthy.
- Prometheus operational metrics reflect DB state.

## Redis/cache decision

Current decision: Redis is not mandatory for readiness while `CACHE_ENABLED=false`.

`/system-status` must expose:

- `redis_up`
- `redis_used`
- `readiness_requires_redis`
- `lock_strategy`

Current lock strategy: `postgres_collection_runs`.

## Queue/backpressure decision

BullMQ is advisory for data-core. Current data-core backpressure is measured from Postgres:

- pending raws
- failed raws
- normalization lag
- analytics lag
- pipeline run counters

## Provider policy

No provider is marked healthy from HTTP success alone. A provider is healthy only when raw collection, normalization and analytics freshness are present.

Drogasil and Droga Raia must remain `BLOCKED` while their latest evidence is `HTTP_403_FORBIDDEN`. No anti-bot bypass is part of this phase.

## Current runtime note

As of 2026-05-25, data-core is `DEGRADED`, not `READY`:

- Docker, Postgres, API, scheduler and worker are healthy.
- `/system-status` returns `DEGRADED`.
- `/ready` correctly returns 503.
- Crypto collection is fresh.
- Crypto normalization and trading analytics are processing.
- Crypto backlog is drained/classified.
- Pague Menos is healthy; Drogasil and Droga Raia remain blocked.
- Disk C: remains critically low and is an operational risk.

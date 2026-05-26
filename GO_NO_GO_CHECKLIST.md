# GO/NO-GO Checklist

## Platform Classification

Current classification: PARTIAL.

## GO Conditions

- Only approved public ports are exposed.
- Postgres is not bound to `0.0.0.0` or public IPv6, or the public bind has a tested host/network block while source bind removal is pending.
- Redis is not publicly exposed.
- Prometheus is not publicly reachable or is behind strong access control.
- Traefik dashboard is not publicly reachable.
- Real baseline backup exists with checksum.
- Baseline restore test has passed.
- Recurring backup and restore-test cadence is automated and validated.
- Prometheus targets are all up or intentionally disabled with documentation.
- Critical containers have restart policy.
- Critical APIs have healthchecks.
- Local production secrets have been removed or documented as temporary.
- Frontend deploy path is reproducible.

## NO-GO Conditions

- Public database listener exists.
- No restore-tested backup.
- Uncommitted local changes are required for production state.
- CI/CD or Coolify deploy path cannot reproduce current runtime.
- Any production change requires notebook-only secrets.
- Prometheus shows critical targets down without owner/decision.

## Current NO-GO Items

- `poupi-crypto-db-1` no longer publishes `5435`; firewall block remains as defense in depth.
- Prometheus no longer publishes `9090`; firewall block remains as defense in depth.
- Traefik dashboard/proxy surface no longer publishes `8080`.
- Coolify direct admin bind is restricted to `127.0.0.1:8000`.
- Coolify realtime ports `6001` and `6002` no longer publish on the host.
- Manual crypto API ports `8002` and `8003` no longer publish on the host.
- Coolify-managed crypto route now returns HTTPS `/health` 200.
- Follow-up pending: `coolify.poupi.com` DNS is invalid and is causing ACME/rate-limit errors in Traefik logs.
- Baseline backup exists at `/opt/backups/20260526T112615Z`; recurring automation is still pending.
- Daily backup and weekly restore-test timers are active and validated; alerting on timer failure is still pending.
- Prometheus stale target `poupi-baby-worker` was removed; old Compose worker must not be started because it targets a separate local Compose DB/Redis. Production worker requires a Coolify-managed app with shared production env.
- `poupi-frontend` and `poupi-brand` have no Git root detected locally.
- Multiple frontend `localhost` fallbacks remain in source.

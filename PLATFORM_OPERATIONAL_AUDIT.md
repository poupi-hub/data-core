# Poupi Platform Operational Audit

Audit date: 2026-05-26.

Scope: infrastructure, operations, security, backups, deployment, observability, and local-machine dependency. No business logic, trading strategy, thresholds, scraping strategy, or runtime-critical behavior was changed.

## Final Classification

PARTIAL.

The platform is mostly cloud-first in practice, but not yet mature enough to call READY. Public database, Prometheus, Traefik dashboard, Coolify realtime and manual crypto API source binds have been removed or restricted. Backup/restore-test failure alerting now exists through systemd `OnFailure` and local Alertmanager. The main blockers are the unresolved `poupi-baby-worker` runtime decision, local/frontend reproducibility gaps, and remaining DNS hygiene for `coolify.poupi.com`.

## Current State

Server-side runtime is active for:

- data-core API, scheduler and worker
- poupi-crypto API and Redis
- poupi-crypto volatile API and Redis
- poupi-baby app/runtime
- poupi-jobs app/runtime
- shared Postgres and Redis
- Prometheus, Grafana and Alertmanager
- Traefik/Coolify

The notebook is no longer the primary runtime. Local Docker was not reachable during audit, and no local running containers/volumes could be confirmed. Local code and secrets still exist and must be cleaned gradually.

## Critical Risks

| Risk | Evidence | Impact | Required Action |
| --- | --- | --- | --- |
| Public Postgres | source bind removed; `poupi-crypto-db-1` now exposes only container port `5432/tcp` | residual risk low; firewall remains as defense in depth | keep Compose `db` service explicit and avoid reintroducing `ports` |
| Public Prometheus | source bind removed; `prometheus` now exposes only container port `9090/tcp` | residual risk low; firewall remains as defense in depth | keep Prometheus internal-only |
| Public Traefik dashboard/surface | source bind removed; no host listener on `8080` in latest validation | residual risk low; Coolify/proxy updates could reintroduce bind | keep source config without public `8080` and validate after updates |
| Backup automation | daily backup and weekly restore-test timers are active and validated; failures trigger local Alertmanager through systemd `OnFailure` | residual risk is end-to-end notification delivery outside Alertmanager | verify receiver delivery path after alert routing is confirmed |
| poupi-baby worker not running | no running `poupi-baby-worker` container or monitoring alias; old compose worker is exited and points at a separate Compose Postgres/Redis stack | worker runtime/queue processing may be absent unless handled elsewhere; starting the old worker risks split-brain queues/data | do not start old Compose worker; deploy a Coolify-managed worker using the same production DB/Redis env if worker processing is required |

## Medium Risks

| Risk | Evidence | Action |
| --- | --- | --- |
| Coolify public surfaces | `8080` and `6001/6002` source binds removed; `8000` now bound to `127.0.0.1` only | residual risk low; keep validating after Coolify updates |
| Runtime containers without healthcheck | scheduler, worker, poupi-baby, poupi-jobs, alertmanager, prometheus | add healthchecks or external synthetic checks |
| Prometheus restart policy | recreated with `unless-stopped` | preserve restart policy in managed compose |
| Local secrets | `.env` and `.env.local` files exist locally | migrate to server-managed secrets, keep examples locally |
| Frontend no Git root | `poupi-frontend` has no `.git` root detected | place under GitHub and CI/CD |
| Frontend localhost fallbacks | many `http://localhost:8000/3001` fallbacks | require env at build/runtime and fail closed in production |

## Architecture Consolidation Target

```text
GitHub
  -> CI/Coolify deploy
    -> /opt/apps/data-core
    -> /opt/apps/poupi-crypto
    -> /opt/apps/poupi-baby
    -> /opt/apps/poupi-jobs
    -> /opt/apps/poupi-frontend
    -> /opt/infra shared Postgres/Redis/networks
    -> /opt/monitoring Prometheus/Grafana/Alertmanager
    -> /opt/backups automated dumps and datasets
    -> /opt/runbooks operational docs
    -> /opt/scripts operational scripts
```

Public edge should be Traefik on `80/443`. Administrative and data services should be reachable only by SSH tunnel, VPN, private network, or authenticated control plane.

## Dangerous Dependencies

- Production-like secrets on local notebook.
- Current runtime state may include uncommitted local repo changes in `data-core`, `poupi-crypto`, and `poupi-baby`.
- `poupi-frontend` and `poupi-brand` are not reproducible from a detected Git root.
- Backup scripts exist but recovery has not been demonstrated.

## Priority Plan

### P0 - Same Day

1. Deploy a proper Coolify-managed `poupi-baby-worker` only if queue processing is required; do not start the old local Compose worker.
2. Verify end-to-end receiver delivery for failed backup/restore-test alerts.
3. Record firewall and public listener baseline after each deploy/reboot.

### P1 - This Week

1. Decide whether `poupi-baby-worker` should be deployed; Prometheus stale target has been removed until then.
2. Add healthchecks or synthetic checks for scheduler, workers, jobs, Prometheus and Alertmanager.
3. Standardize `/opt/backups`, `/opt/runbooks`, `/opt/scripts`, `/opt/monitoring`.
4. Create remote-only secret inventory by key name, not value.
5. Move frontend into reproducible Git/CI/CD flow.

### P2 - Next Iteration

1. Remove local real `.env` files after remote validation.
2. Replace frontend localhost production fallbacks.
3. Normalize Compose naming and log rotation.
4. Add scheduled backup jobs with retention and restore-test cadence.
5. Build a single operational dashboard for platform readiness.

## Deliverables Created

- `CLOUD_MIGRATION_PLAN.md`
- `LOCAL_MINIMAL_SETUP.md`
- `SERVER_OPERATIONS_RUNBOOK.md`
- `BACKUP_AND_RESTORE.md`
- `INCIDENT_RESPONSE.md`
- `DEPLOY_AND_ROLLBACK.md`
- `GO_NO_GO_CHECKLIST.md`
- `FRONTEND_STABILIZATION_PLAN.md`
- `scripts/remote-health.sh`
- `scripts/remote-logs.sh`
- `scripts/remote-deploy.sh`
- `scripts/remote-backup.sh`

## Validation Performed

- Remote Docker inventory captured.
- Remote volumes and networks captured.
- Public listeners inspected via `ss`.
- Docker restart/health/privileged state inspected.
- Prometheus health checked.
- Prometheus targets checked.
- Postgres readiness checked.
- Redis reachability checked where possible.
- Backup directories inspected.
- Baseline backup created at `/opt/backups/20260526T112615Z`.
- Shared Postgres and crypto Postgres dumps restored into temporary isolated containers.
- Host firewall/DOCKER-USER rules applied and persisted for `5435`, `9090`, `8080`, `6001`, and `6002`.
- External checks confirmed `5435`, `9090`, `8080`, `6001`, `6002`, `8002`, and `8003` closed while `443` remained reachable.
- Prometheus was recreated without host port publication and with `restart=unless-stopped`.
- `poupi-crypto-db-1` was recreated from an explicit Compose `db` service without host port publication, preserving the existing `poupi-crypto_pgdata` volume.
- Stale Prometheus target `poupi-baby-worker` was removed after evidence showed no running worker container or network alias. Remaining active targets are all `up`.
- `coolify-proxy` was recreated without host port `8080`.
- `coolify` was recreated with direct admin bind restricted to `127.0.0.1:8000`.
- `coolify-realtime` was recreated on the same image tag without host ports `6001/6002`.
- Manual crypto API stacks were recreated without host ports `8002/8003`; both stayed healthy and Prometheus targets remained `up`.
- Coolify-managed `poupi-crypto` HTTPS route now returns `200 {"status":"ok"}` through Traefik.
- Remaining Traefik ACME errors are for `coolify.poupi.com`, which has no valid DNS record and has hit Let's Encrypt failed-authorization rate limiting; this is separate from the crypto route.
- Automated daily backup and weekly restore-test systemd timers were installed and validated successfully.
- Backup and restore-test services now have systemd `OnFailure` hooks to local Alertmanager; handler script passed dry-run JSON validation and Alertmanager readiness returned `OK`.
- `poupi-baby-worker` decision recorded: old Compose worker must not be started because it targets a separate local Compose database/Redis stack; create a production worker app with shared production env instead.
- Local Git and frontend structure inspected.
- Frontend safe env examples and `check:prod-env` guardrail were added locally; the guardrail currently fails by design until localhost fallbacks are remediated.
- New shell scripts syntax-checked with remote `bash -n`.

## Explicit Non-Actions

- No production container was restarted.
- No Compose file was applied.
- Firewall rules were changed only after a verified backup and restore test; snapshots were saved under `/opt/backups/firewall`.
- No volume was deleted.
- No `.env` value was printed.
- No business/trading/scraping logic was changed.

# Deploy And Rollback

## Deployment Principles

- Deploy from Git, CI/CD, or Coolify.
- Do not deploy uncommitted local changes.
- Do not deploy from a notebook-only state.
- Confirm backup posture before database migrations.
- Keep rollback path known before deploy.

## Pre-Deploy Checklist

```bash
git status --short --branch
git log -1 --oneline
ssh poupi "docker ps --filter health=unhealthy --format '{{.Names}}'"
ssh poupi "curl -fsS http://127.0.0.1:9090/-/healthy"
ssh poupi "curl -fsS http://127.0.0.1:9090/api/v1/targets | jq -r '.data.activeTargets[] | [.labels.job,.health,.lastError] | @tsv'"
```

NO-GO if:

- database backup is required and no validated backup exists,
- any critical service is unhealthy,
- public database exposure is unresolved for a deploy touching database credentials or schema,
- local repo has uncommitted changes that are part of the intended deploy.

## Deploy Paths

### Coolify-managed data-core

Use Coolify deploy controls or the existing Coolify queue workflow documented in `ai/RUNBOOK.md`.

After deploy:

```bash
ssh poupi "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | grep dvq6"
ssh poupi "curl -fsS http://127.0.0.1:9090/api/v1/targets | jq -r '.data.activeTargets[] | [.labels.job,.health,.lastError] | @tsv'"
```

### Compose-managed apps

Example:

```bash
ssh poupi "cd /opt/apps/poupi-crypto && git pull --ff-only && docker compose build api && docker compose up -d api"
```

Use the actual project path and service name.

## Rollback

Rollback must use a known previous image, Git commit, or Coolify deployment.

Generic compose rollback:

```bash
ssh poupi "cd /opt/apps/<service> && git checkout <known-good-commit> && docker compose up -d --build"
```

After rollback:

```bash
ssh poupi "docker ps --filter health=unhealthy --format '{{.Names}}'"
ssh poupi "docker logs --tail 100 <container>"
```

## Post-Deploy Validation

- App health endpoint returns 200.
- Prometheus target is up.
- Logs do not show migration/runtime crash loops.
- Queue/scheduler metrics are updating.
- No new public ports are introduced.

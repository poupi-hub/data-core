# Local Minimal Setup

The notebook is a development and operations client only. It should not run continuous production workloads.

## Required Locally

- Git
- Code editor
- SSH client
- Access to GitHub
- Optional: Docker Desktop for isolated tests only
- Optional: Node/Python tooling for project-specific development only

## Not Required Locally For Continuous Runtime

- PostgreSQL
- Redis
- schedulers
- workers
- scrapers
- Prometheus
- Grafana
- Alertmanager
- production data volumes
- production `.env` files

## Local Secret Policy

Do not keep production secrets on the notebook unless actively needed for a short, documented operation.

Allowed locally:

- `.env.example`
- `.env.local.example`
- SSH config without embedded passwords
- API placeholders

To retire local secrets:

```powershell
# Confirm server-side envs exist before doing this.
Move-Item .env .env.local.backup.DO_NOT_COMMIT
```

Never commit:

```text
.env
.env.*
!.env.example
!.env.local.example
```

## Recommended Local Workflow

```text
edit locally -> run focused tests if needed -> commit -> push -> remote deploy/CI -> remote health check
```

## Remote Operation Commands

From a local clone:

```bash
scripts/remote-health.sh
scripts/remote-logs.sh data-core api
scripts/remote-deploy.sh poupi-crypto
scripts/remote-backup.sh inventory
```

## Local Cleanup Checklist

- Real `.env` files replaced with examples or encrypted vault workflow.
- Docker Desktop not required for daily operation.
- No local scheduler process configured at login/startup.
- No local Postgres/Redis services running continuously.
- Frontend `.env.local` files contain non-secret remote URLs only.

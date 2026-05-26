# Backup And Restore

Backup status as of 2026-05-26: baseline backup created and restore-tested.

Initial audit found scripts under `/opt/infra/shared/backups`, but no actual backup dump files under `/opt/backups`, `/opt/infra/shared/backups`, or `/data/coolify/backups`. After the audit, a baseline backup was created and tested.

## Verified Baseline

Backup directory:

```text
/opt/backups/20260526T112615Z
```

Artifacts:

| Artifact | Size | Validation |
| --- | ---: | --- |
| `multi_project_infra_pg_dumpall.sql.gz` | 29M | checksum OK, restore test passed |
| `poupi_crypto_pg_dumpall.sql.gz` | 34K | checksum OK, restore test passed |
| `poupi_crypto_signal_dataset.tar.gz` | 80K | checksum OK |
| `poupi_crypto_volatile_signal_dataset.tar.gz` | 5.1K | checksum OK |
| `SHA256SUMS` | 532B | `sha256sum -c` passed |

Restore test results:

- Shared Postgres dump restored into temporary `restore-test-postgres`.
- Restored databases observed: `analytics_db`, `data_core_db`, `poupi_baby_db`, `poupi_crypto_db`, `poupi_jobs_db`, `trading_bot_db`.
- Crypto Postgres dump restored into temporary `restore-test-postgres`.
- Restored databases observed: `poupi_crypto`, `poupi_crypto_volatile`.
- Temporary restore container and volume were removed after each test.

Note: `pg_dumpall` emitted `ERROR: role "postgres" already exists` during restore into a fresh image. This is expected when restoring global roles over the default bootstrap role and did not block database restore validation.

## Automation

Automated server jobs were installed and validated on 2026-05-26:

| Unit | Schedule | Purpose | Status |
| --- | --- | --- | --- |
| `poupi-backup.timer` | daily `03:20 UTC` | creates PostgreSQL dumps and signal dataset archives | active, validated |
| `poupi-restore-test.timer` | Sunday `04:20 UTC` | restore-tests the latest successful backup in an isolated Postgres container | active, validated |

Failure alerting:

- `poupi-backup.service` and `poupi-restore-test.service` use systemd `OnFailure`.
- The failure handler is `/opt/scripts/poupi-systemd-failure-alert.sh`.
- The handler posts a `PoupiSystemdUnitFailed` critical alert to local Alertmanager at `http://127.0.0.1:9093/api/v2/alerts`.
- Dry-run validation completed with valid JSON; Alertmanager readiness returned `OK`.

Server scripts:

```text
/opt/scripts/poupi-backup.sh
/opt/scripts/poupi-restore-test.sh
/opt/scripts/poupi-systemd-failure-alert.sh
```

Latest systemd validation:

```text
poupi-backup.service -> SUCCESS, backup /opt/backups/20260526T121616Z
poupi-restore-test.service -> SUCCESS, restore_test_completed=20260526T121646Z
```

Logs are written under:

```text
/opt/backups/logs
```

Check automation:

```bash
ssh poupi "systemctl list-timers 'poupi-*'"
ssh poupi "systemctl status poupi-backup.service --no-pager -l"
ssh poupi "systemctl status poupi-restore-test.service --no-pager -l"
ssh poupi "systemctl show poupi-backup.service -p OnFailure --value"
ssh poupi "systemctl show poupi-restore-test.service -p OnFailure --value"
```

## Critical Assets

Databases:

- `multi_project_infra-postgres-1`
- `poupi-crypto-db-1`
- `coolify-db`

Volumes and datasets:

- `multi_project_infra_postgres-data`
- `poupi-crypto_pgdata`
- `poupi-baby_postgres-data`
- `poupi-jobs_pgdata`
- `poupi_crypto_signal_dataset`
- `poupi_crypto_volatile_signal_dataset`
- `dvq6dwsagsw4p4oqwuw7bak9_runtime-data`
- `dvq6dwsagsw4p4oqwuw7bak9_runtime-logs`
- `q11p1efg13of6ujrfgu25lal_grafana-data`
- `prometheus-data`

Configs:

- `/opt/apps/*/.env`
- `/opt/infra/.env`
- `/data/coolify/source/.env`
- Compose files under `/opt/apps` and `/opt/infra`

## Backup Command Examples

Create a timestamped backup directory:

```bash
ssh poupi "sudo mkdir -p /opt/backups/$(date -u +%Y%m%dT%H%M%SZ)"
```

Shared Postgres:

```bash
ssh poupi "ts=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p /opt/backups/$ts; docker exec multi_project_infra-postgres-1 pg_dumpall -U postgres | gzip > /opt/backups/$ts/multi_project_infra_pg_dumpall.sql.gz; sha256sum /opt/backups/$ts/*.gz > /opt/backups/$ts/SHA256SUMS"
```

Crypto Postgres:

```bash
ssh poupi "ts=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p /opt/backups/$ts; docker exec poupi-crypto-db-1 pg_dumpall -U postgres | gzip > /opt/backups/$ts/poupi_crypto_pg_dumpall.sql.gz; sha256sum /opt/backups/$ts/*.gz > /opt/backups/$ts/SHA256SUMS"
```

Dataset volume archive:

```bash
ssh poupi "ts=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p /opt/backups/$ts; docker run --rm -v poupi_crypto_signal_dataset:/data:ro -v /opt/backups/$ts:/backup alpine tar -czf /backup/poupi_crypto_signal_dataset.tar.gz -C /data .; sha256sum /opt/backups/$ts/*.tar.gz >> /opt/backups/$ts/SHA256SUMS"
```

## Restore Test Procedure

Never restore into production as a test.

1. Create an isolated restore network and temporary Postgres.
2. Load the dump.
3. Run sanity checks.
4. Destroy only the temporary container and temporary volume.
5. Record result.

Example:

```bash
ssh poupi "docker volume create restore-test-pgdata"
ssh poupi "docker run -d --name restore-test-postgres -e POSTGRES_PASSWORD=restore-test -v restore-test-pgdata:/var/lib/postgresql/data postgres:16-alpine"
ssh poupi "sleep 10 && docker exec restore-test-postgres pg_isready -U postgres"
ssh poupi "gzip -dc /opt/backups/<timestamp>/multi_project_infra_pg_dumpall.sql.gz | docker exec -i restore-test-postgres psql -U postgres"
ssh poupi "docker exec restore-test-postgres psql -U postgres -c '\\l'"
ssh poupi "docker rm -f restore-test-postgres && docker volume rm restore-test-pgdata"
```

## Retention Policy

Recommended baseline:

- Daily: 7 days
- Weekly: 4 weeks
- Monthly: 6 months

Retention deletion is allowed only after:

- the newest backup has a restore test,
- checksums exist,
- backup age exceeds retention,
- command targets only `/opt/backups`.

## Disaster Recovery Checklist

- Identify failed service and affected volume/database.
- Stop writes if data consistency is at risk.
- Snapshot current broken state before restore.
- Restore into isolated environment first.
- Validate application migrations and schema.
- Promote restored data only with explicit approval.
- Record incident timeline and commands.

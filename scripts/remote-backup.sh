#!/usr/bin/env bash
set -euo pipefail

HOST="${POUPI_HOST:-poupi}"
ACTION="${1:-inventory}"

case "$ACTION" in
  inventory)
    ssh "$HOST" '
set -e
echo "== backup files =="
find /opt/backups /opt/infra/shared/backups /data/coolify/backups -maxdepth 3 -type f 2>/dev/null | sort || true
echo "== critical volumes =="
docker volume ls --format "{{.Name}}" | grep -E "postgres|pgdata|redis|signal|runtime|grafana|prometheus|coolify" || true
'
    ;;
  create)
    ssh "$HOST" '
set -e
ts=$(date -u +%Y%m%dT%H%M%SZ)
dest="/opt/backups/$ts"
mkdir -p "$dest"

echo "== dumping shared postgres =="
docker exec multi_project_infra-postgres-1 pg_dumpall -U postgres | gzip > "$dest/multi_project_infra_pg_dumpall.sql.gz"

echo "== dumping crypto postgres =="
docker exec poupi-crypto-db-1 pg_dumpall -U postgres | gzip > "$dest/poupi_crypto_pg_dumpall.sql.gz"

echo "== archiving signal datasets =="
docker run --rm -v poupi_crypto_signal_dataset:/data:ro -v "$dest":/backup alpine tar -czf /backup/poupi_crypto_signal_dataset.tar.gz -C /data .
docker run --rm -v poupi_crypto_volatile_signal_dataset:/data:ro -v "$dest":/backup alpine tar -czf /backup/poupi_crypto_volatile_signal_dataset.tar.gz -C /data .

sha256sum "$dest"/* > "$dest/SHA256SUMS"
echo "$dest"
'
    ;;
  restore-test)
    BACKUP_DIR="${2:-}"
    if [[ -z "$BACKUP_DIR" ]]; then
      echo "Usage: scripts/remote-backup.sh restore-test /opt/backups/<timestamp>"
      exit 2
    fi
    ssh "$HOST" "
set -e
test -f '$BACKUP_DIR/multi_project_infra_pg_dumpall.sql.gz'
docker volume create restore-test-pgdata >/dev/null
docker rm -f restore-test-postgres >/dev/null 2>&1 || true
docker run -d --name restore-test-postgres -e POSTGRES_PASSWORD=restore-test -v restore-test-pgdata:/var/lib/postgresql/data postgres:16-alpine >/dev/null
sleep 10
docker exec restore-test-postgres pg_isready -U postgres
gzip -dc '$BACKUP_DIR/multi_project_infra_pg_dumpall.sql.gz' | docker exec -i restore-test-postgres psql -U postgres >/tmp/restore-test.log
docker exec restore-test-postgres psql -U postgres -c '\\l'
docker rm -f restore-test-postgres >/dev/null
docker volume rm restore-test-pgdata >/dev/null
echo 'restore test passed for shared postgres dump'
"
    ;;
  *)
    echo "Usage: scripts/remote-backup.sh {inventory|create|restore-test /opt/backups/<timestamp>}"
    exit 2
    ;;
esac

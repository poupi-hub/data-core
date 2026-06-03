#!/usr/bin/env bash
# backup.sh — Backup semanal dos bancos Neon
# Instalar no cron do VPS:
#   crontab -e
#   0 3 * * 0 /opt/data-core/deploy/backup.sh >> /var/log/poupi-backup.log 2>&1
set -euo pipefail

BACKUP_DIR="/opt/backups/$(date +%Y-%m-%d)"
mkdir -p "${BACKUP_DIR}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Iniciando backup..."

# Carregar .env para obter DATABASE_URLs
# shellcheck disable=SC1091
source /opt/data-core/.env

# Backup data_core
echo "  data_core..."
pg_dump "${DATABASE_URL}" \
    --no-owner --no-acl \
    --format=custom \
    -f "${BACKUP_DIR}/data_core.dump"

# Backup poupi_crypto (lê .env do crypto)
CRYPTO_DB_URL=$(grep "^DATABASE_URL=" /opt/poupi-crypto/.env | cut -d= -f2-)
echo "  poupi_crypto..."
pg_dump "${CRYPTO_DB_URL}" \
    --no-owner --no-acl \
    --format=custom \
    -f "${BACKUP_DIR}/poupi_crypto.dump"

# Backup poupi_baby (URL precisa ser definida aqui ou lida de outro .env)
if [[ -n "${BABY_DATABASE_URL:-}" ]]; then
    echo "  poupi_baby..."
    pg_dump "${BABY_DATABASE_URL}" \
        --no-owner --no-acl \
        --format=custom \
        -f "${BACKUP_DIR}/poupi_baby.dump"
fi

# Compactar
tar czf "/opt/backups/backup_$(date +%Y%m%d).tar.gz" "${BACKUP_DIR}"
rm -rf "${BACKUP_DIR}"

# Manter apenas últimos 8 backups (2 meses)
ls -t /opt/backups/backup_*.tar.gz | tail -n +9 | xargs -r rm

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup concluído: /opt/backups/backup_$(date +%Y%m%d).tar.gz"

#!/usr/bin/env bash
# deploy.sh — Atualizar data-core no VPS sem downtime
# Executar no VPS como root ou app com sudo
# Uso: bash deploy.sh [branch]
set -euo pipefail

BRANCH="${1:-main}"
APP_DIR="/opt/data-core"
SERVICE="data-core"

echo "==> Atualizando código (branch: ${BRANCH})..."
cd "${APP_DIR}"
sudo -u app git fetch origin
sudo -u app git checkout "${BRANCH}"
sudo -u app git pull origin "${BRANCH}"

echo "==> Atualizando dependências..."
sudo -u app .venv/bin/pip install -r requirements.txt -q

echo "==> Rodando migrations..."
sudo -u app .venv/bin/alembic upgrade head

echo "==> Reiniciando serviços..."
systemctl restart ${SERVICE} ${SERVICE}-scheduler ${SERVICE}-worker

echo "==> Aguardando health check..."
sleep 5
for i in $(seq 1 12); do
    if curl -sf http://localhost:8000/health | grep -q '"status":"ok"'; then
        echo "✅ data-core online."
        break
    fi
    echo "   Aguardando... (${i}/12)"
    sleep 5
done

systemctl status ${SERVICE} --no-pager -l | tail -5
echo "==> Deploy concluído."

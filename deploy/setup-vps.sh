#!/usr/bin/env bash
# setup-vps.sh — Provisionamento inicial do VPS Ubuntu 24.04
# Executar como root: bash setup-vps.sh
# Substitua <SEU_DOMINIO> antes de rodar.
set -euo pipefail

DOMAIN="${DOMAIN:-<SEU_DOMINIO>}"
APP_USER="app"
PY_VERSION="3.12"

echo "==> Atualizando sistema..."
apt-get update -qq && apt-get upgrade -y -qq

echo "==> Instalando dependências base..."
apt-get install -y -qq \
    git curl wget unzip \
    python${PY_VERSION} python${PY_VERSION}-venv python${PY_VERSION}-dev \
    build-essential libpq-dev \
    ufw fail2ban \
    logrotate

echo "==> Instalando Caddy..."
apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
    tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq && apt-get install -y -qq caddy

echo "==> Instalando Playwright deps (Chromium headless)..."
apt-get install -y -qq \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libpangocairo-1.0-0

echo "==> Criando usuário app..."
id -u ${APP_USER} &>/dev/null || useradd -m -s /bin/bash ${APP_USER}
mkdir -p /opt/data-core /opt/poupi-crypto /var/log/caddy
chown -R ${APP_USER}:${APP_USER} /opt/data-core /opt/poupi-crypto

echo "==> Configurando ufw..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw allow 80/tcp comment "HTTP (Caddy redirect)"
ufw allow 443/tcp comment "HTTPS"
ufw --force enable

echo "==> Configurando fail2ban..."
systemctl enable fail2ban --now

echo "==> Configurando logrotate para Caddy..."
cat > /etc/logrotate.d/caddy << 'EOF'
/var/log/caddy/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 caddy caddy
    postrotate
        systemctl reload caddy > /dev/null 2>&1 || true
    endscript
}
EOF

echo "==> Instalando serviços systemd data-core..."
cp /opt/data-core/deploy/data-core.service /etc/systemd/system/
cp /opt/data-core/deploy/data-core-scheduler.service /etc/systemd/system/
cp /opt/data-core/deploy/data-core-worker.service /etc/systemd/system/

echo "==> Instalando serviço systemd crypto..."
cp /opt/poupi-crypto/deploy/crypto-api.service /etc/systemd/system/

echo "==> Copiando Caddyfile..."
cp /opt/data-core/deploy/Caddyfile /etc/caddy/Caddyfile
sed -i "s/<SEU_DOMINIO>/${DOMAIN}/g" /etc/caddy/Caddyfile

echo "==> Recarregando systemd..."
systemctl daemon-reload
systemctl enable data-core data-core-scheduler data-core-worker crypto-api caddy

echo ""
echo "✅ VPS provisionado."
echo "Próximos passos:"
echo "  1. Criar /opt/data-core/.env com variáveis cloud"
echo "  2. Criar /opt/poupi-crypto/.env com variáveis cloud"
echo "  3. cd /opt/data-core && git clone ... . && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt"
echo "  4. cd /opt/poupi-crypto && git clone ... . && python3.12 -m venv .venv && .venv/bin/pip install -e ."
echo "  5. .venv/bin/playwright install chromium"
echo "  6. systemctl start data-core crypto-api"
echo "  7. systemctl start caddy"

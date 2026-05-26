#!/usr/bin/env bash
set -euo pipefail

HOST="${POUPI_HOST:-poupi}"
SERVICE="${1:-}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/remote-deploy.sh <service>

Supported compose services:
  poupi-crypto
  poupi-crypto-volatile
  poupi-baby
  poupi-jobs

For data-core, use the Coolify deploy flow documented in ai/RUNBOOK.md.
USAGE
}

if [[ -z "$SERVICE" ]]; then
  usage
  exit 2
fi

case "$SERVICE" in
  data-core)
    echo "data-core is Coolify-managed. Use Coolify deploy or ai/RUNBOOK.md."
    exit 2
    ;;
  poupi-crypto)
    REMOTE_DIR="/opt/apps/poupi-crypto"
    COMPOSE_FILE="docker-compose.yml"
    ;;
  poupi-crypto-volatile)
    REMOTE_DIR="/opt/apps/poupi-crypto"
    COMPOSE_FILE="docker-compose.volatile.yml"
    ;;
  poupi-baby)
    REMOTE_DIR="/opt/apps/poupi-baby"
    COMPOSE_FILE="docker-compose.yml"
    ;;
  poupi-jobs)
    REMOTE_DIR="/opt/apps/poupi-jobs"
    COMPOSE_FILE="docker-compose.yml"
    ;;
  *)
    usage
    exit 2
    ;;
esac

ssh "$HOST" "
set -e
cd '$REMOTE_DIR'
echo '== pre-deploy status =='
docker compose -f '$COMPOSE_FILE' ps || true
echo '== updating source if git repo exists =='
if [ -d .git ]; then git status --short --branch && git pull --ff-only; fi
echo '== build and up =='
docker compose -f '$COMPOSE_FILE' build
docker compose -f '$COMPOSE_FILE' up -d
echo '== post-deploy status =='
docker compose -f '$COMPOSE_FILE' ps
"

#!/usr/bin/env bash
set -euo pipefail

HOST="${POUPI_HOST:-poupi}"
SERVICE="${1:-}"
CONTAINER_OR_COMPONENT="${2:-}"
TAIL_LINES="${TAIL_LINES:-200}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/remote-logs.sh <service> [component]

Examples:
  scripts/remote-logs.sh data-core api
  scripts/remote-logs.sh data-core scheduler
  scripts/remote-logs.sh poupi-crypto api
  scripts/remote-logs.sh poupi-crypto redis
  scripts/remote-logs.sh poupi-baby
  scripts/remote-logs.sh poupi-jobs
USAGE
}

if [[ -z "$SERVICE" ]]; then
  usage
  exit 2
fi

case "$SERVICE:$CONTAINER_OR_COMPONENT" in
  data-core:api) TARGET="api-dvq6dwsagsw4p4oqwuw7bak9" ;;
  data-core:scheduler) TARGET="scheduler-dvq6dwsagsw4p4oqwuw7bak9" ;;
  data-core:worker) TARGET="worker-dvq6dwsagsw4p4oqwuw7bak9" ;;
  poupi-crypto:api|"poupi-crypto:") TARGET="poupi-crypto-api-1" ;;
  poupi-crypto:redis) TARGET="poupi-crypto-redis-1" ;;
  poupi-crypto:db) TARGET="poupi-crypto-db-1" ;;
  poupi-crypto-volatile:api|"poupi-crypto-volatile:") TARGET="poupi-crypto-volatile-api-1" ;;
  poupi-baby:*) TARGET="dfmhxr9vn96wxbno98b0i1ik" ;;
  poupi-jobs:*) TARGET="o108ydw8tmw73aicy5wbdpvn" ;;
  prometheus:*) TARGET="prometheus" ;;
  grafana:*) TARGET="grafana" ;;
  alertmanager:*) TARGET="alertmanager" ;;
  *) TARGET="$SERVICE" ;;
esac

ssh "$HOST" "docker ps --format '{{.Names}}' | grep -m1 '$TARGET' | xargs -r docker logs --tail '$TAIL_LINES'"

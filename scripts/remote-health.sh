#!/usr/bin/env bash
set -euo pipefail

HOST="${POUPI_HOST:-poupi}"

ssh "$HOST" '
set -e
echo "== host =="
hostname
date -Is

echo "== containers =="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo "== unhealthy containers =="
docker ps --filter health=unhealthy --format "{{.Names}}" || true

echo "== public listeners =="
ss -tulpn | awk '"'"'NR==1 || /LISTEN/'"'"'

echo "== core dependencies =="
docker exec multi_project_infra-postgres-1 pg_isready -U postgres || true
docker exec poupi-crypto-db-1 pg_isready -U postgres || true
docker exec multi_project_infra-redis-1 redis-cli ping || true
docker exec poupi-crypto-redis-1 redis-cli ping || true

echo "== observability =="
docker exec prometheus wget -qO- http://127.0.0.1:9090/-/healthy || true
echo
curl -fsS --max-time 5 http://127.0.0.1:9093/-/healthy || true
echo

echo "== prometheus targets =="
docker exec prometheus wget -qO- http://127.0.0.1:9090/api/v1/targets \
  | jq -r '"'"'.data.activeTargets[] | [.labels.job,.health,.scrapeUrl,.lastError] | @tsv'"'"' || true
'

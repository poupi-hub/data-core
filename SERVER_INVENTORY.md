# Server Inventory

Data: 2026-05-31

Host auditado:

```text
ssh target = poupi
hostname = ubuntu-4gb-hel1-1
ip = 65.109.239.250
kernel = Linux 6.8.0-111-generic x86_64
uptime observado = 16 days, 17:37
```

## Containers Observados

Comando:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
```

Principais containers:

| Container | Papel inferido | Status observado |
|---|---|---|
| `1005290a3c86_api-dvq6dwsagsw4p4oqwuw7bak9-133137020220` | data-core API | Up healthy |
| `scheduler-dvq6dwsagsw4p4oqwuw7bak9-133137072047` | data-core scheduler | Up healthy |
| `worker-dvq6dwsagsw4p4oqwuw7bak9-133137188098` | data-core worker | Up healthy |
| `multi_project_infra-postgres-1` | Postgres compartilhado | Up healthy |
| `multi_project_infra-redis-1` | Redis compartilhado | Up healthy |
| `poupi-baby-worker-prod` | Poupi Baby worker | Up healthy |
| `dfmhxr9vn96wxbno98b0i1ik-233133268719` | Poupi Baby API provável | Up |
| `poupi-crypto-api-1` | Crypto API | Up healthy |
| `poupi-crypto-volatile-api-1` | Crypto volatile API | Up healthy |
| `prometheus` | Prometheus | Up |
| `alertmanager` | Alertmanager | Up |
| `grafana-q11p1efg13of6ujrfgu25lal` | Grafana | Up healthy |
| `coolify`, `coolify-proxy`, `coolify-db`, `coolify-redis` | Coolify | Up |

## Docker Compose / Coolify

Comando:

```bash
docker compose ls
```

Stacks observadas:

| Stack | Config |
|---|---|
| `dvq6dwsagsw4p4oqwuw7bak9` | `/data/coolify/applications/dvq6dwsagsw4p4oqwuw7bak9/docker-compose.yaml` |
| `multi_project_infra` | `/opt/infra/docker-compose.prod.yml` |
| `poupi-crypto` | `/opt/apps/poupi-crypto/docker-compose.yml`, `docker-compose.volatile.yml` |
| `poupi-baby-worker` | `/opt/apps/poupi-baby-worker/docker-compose.yml` |
| `coolify-proxy` | `/data/coolify/proxy/docker-compose.yml` |
| `source` | `/data/coolify/source/docker-compose.yml`, `docker-compose.prod.yml` |

## Networks

Redes relevantes:

```text
coolify
infra_internal
dvq6dwsagsw4p4oqwuw7bak9
poupi-baby_default
poupi-crypto_default
poupi-jobs_default
poupi-monitoring
```

## Volumes

Volumes relevantes:

```text
multi_project_infra_postgres-data
multi_project_infra_redis-data
dvq6dwsagsw4p4oqwuw7bak9_runtime-data
dvq6dwsagsw4p4oqwuw7bak9_runtime-logs
poupi-baby_postgres-data
poupi-crypto_pgdata
poupi-jobs_pgdata
prometheus-data
q11p1efg13of6ujrfgu25lal_grafana-data
```

## Portas Publicas

Portas publicas observadas:

```text
80/tcp -> coolify-proxy
443/tcp, 443/udp -> coolify-proxy
127.0.0.1:8000 -> coolify
127.0.0.1:9093 -> alertmanager
```

## Arquivos `.env`

Arquivos encontrados sem exibir conteudo:

```text
/data/coolify/source/.env
/data/coolify/applications/dvq6dwsagsw4p4oqwuw7bak9/.env
/opt/apps/poupi-baby/.env
/opt/apps/poupi-crypto/.env
/opt/apps/poupi-jobs/.env
/opt/infra/.env
```

## Observacoes Criticas

- O app Coolify `dvq6dwsagsw4p4oqwuw7bak9` e o data-core.
- A imagem do data-core observada inicialmente estava defasada: registry remoto nao continha `real_estate.direct_agencies` nem coletores `jobs.*`.
- Apos hot patch em containers, registry passou a reconhecer os novos coletores, mas isso ainda nao e deploy duravel de imagem.
- `/health` do data-core estava `degraded` por Redis Upstash com limite estourado.
- Apos replay longo de Real Estate, o servidor passou a aceitar TCP/22 mas nao completou banner SSH dentro de 10s, indicando degradacao operacional sob carga.


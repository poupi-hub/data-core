# Playbooks Operacionais — Poupi Ecossistema

> Gerado em 2026-06-13 com base em evidências de runtime VPS.  
> VPS: `ssh poupi` → `65.109.239.250`

---

## PB-01 — API Indisponível (data-core ou poupi-crypto)

**Sintoma:** `/health` ou `/ready` retorna erro / timeout / 503.

### Diagnóstico

```bash
# 1. Verificar status do container
docker inspect api-dvq6dwsagsw4p4oqwuw7bak9-1781200620800 --format '{{.State.Health.Status}} {{.State.Status}}'
docker inspect poupi-crypto-api-1 --format '{{.State.Health.Status}} {{.State.Status}}'

# 2. Logs do container
docker logs api-dvq6dwsagsw4p4oqwuw7bak9-1781200620800 --tail 50 2>&1 | grep -E 'ERROR|Traceback|FATAL'
docker logs poupi-crypto-api-1 --tail 50 2>&1 | grep -E 'ERROR|Traceback|FATAL'

# 3. Verificar DB e Redis
curl -fsS http://127.0.0.1:8000/health   # data-core
curl -fsS http://127.0.0.1:8002/ready    # poupi-crypto
```

### Ação por causa

| Causa | Ação |
|---|---|
| Container stopped (crash loop) | `docker restart <container>` |
| OOM (memória) | `docker stats` → aumentar `mem_limit` no compose |
| DB indisponível | Ver PB-03 (Postgres) |
| Redis indisponível | Ver PB-04 (Redis) |
| Erro de código (Traceback) | `docker logs --tail 200` → corrigir e redeployar |

### Restart rápido

```bash
# data-core (via Coolify — redeployar pela UI ou:)
cd /data/coolify/applications/dvq6dwsagsw4p4oqwuw7bak9
docker compose restart api

# poupi-crypto
cd /opt/apps/poupi-crypto
docker compose restart api
```

### Rollback

```bash
# Reverter para imagem anterior (data-core)
docker compose -f docker-compose.yaml up -d --no-deps api --build  # rebuild

# poupi-crypto: rollback para commit anterior
cd /opt/apps/poupi-crypto
git log --oneline -5
git checkout <commit>
docker compose up -d --no-deps api
```

---

## PB-02 — Telegram Falhando

**Sintoma:** `WARNING: Telegram inline send failed` nos logs / alertas não chegam.

### Diagnóstico

```bash
# 1. Verificar conectividade do bot
TOKEN=$(docker inspect poupi-crypto-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^TELEGRAM_BOT_TOKEN=' | cut -d= -f2-)
curl -fsS "https://api.telegram.org/bot${TOKEN}/getMe"

# 2. Testar envio ao chat pessoal
curl -fsS "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d "chat_id=5912543085&text=ping $(date)"

# 3. Testar envio ao grupo executive
curl -fsS "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d "chat_id=-1003913617610&text=ping"

# 4. Verificar rate limits
curl -fsS "https://api.telegram.org/bot${TOKEN}/getUpdates?limit=1"
```

### Causas conhecidas

| Causa | Evidência | Ação |
|---|---|---|
| Bot não é membro do grupo | `getChat` falha | Adicionar bot `@poupi_baby_bot` ao grupo como admin |
| Rate limit (429) | Resposta `{"error_code":429}` | Aguardar `retry_after` segundos |
| Chat ID errado | `{"ok":false,"description":"Bad Request"}` | Corrigir `LOBO_MIRROR_EXECUTIVE_CHAT_ID` no `.env` |
| Token expirado/inválido | `getMe` falha | Rotacionar token via @BotFather |

### Situação atual (2026-06-13)

- `poupi_baby_bot` funciona para chat pessoal (5912543085) ✅
- Grupo executive (`-1003913617610`): bot **provavelmente não é membro** — `getChat` retorna vazio
- **Ação pendente:** Adicionar `@poupi_baby_bot` ao grupo `-1003913617610` como admin

### Rollback

Não há rollback — reconfigurar o `TELEGRAM_BOT_TOKEN` ou `EXECUTIVE_CHAT_ID` e reiniciar o worker.

---

## PB-03 — SL Não Criado na MEXC

**Sintoma:** Trade em `pending_entry` sem `sl_order_id`; posição real sem proteção.

### Diagnóstico

```bash
# 1. Verificar trades pending
API_KEY=$(docker inspect poupi-crypto-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^API_KEY=' | cut -d= -f2-)
curl -fsS 'http://127.0.0.1:8002/api/v1/lobo-mirror/origin/trades' -H "X-API-Key: $API_KEY"

# 2. Verificar saldo na conta CAV
curl -fsS https://background-prince-announces-are.trycloudflare.com/balance

# 3. Verificar kill switch
curl -fsS 'http://127.0.0.1:8002/ready' | python3 -m json.tool | grep -A5 'origin_real'

# 4. Logs do worker (executor)
docker logs poupi-crypto-worker-1 --tail 100 | grep -E 'SL|stop_loss|sl_order|MEXC|executor|pending'
```

### Causas e ações

| Causa | Ação |
|---|---|
| `Saldo zero — conta cav pausada 60 min` | Depositar USDT na conta MEXC CAV; aguardar próximo sinal |
| `EXCHANGE_SANDBOX=true` | Confirmar que é intencional; alterar para `false` apenas após validação |
| `CRYPTO_REAL_ORDER_ENABLED=false` | Confirmar permissão; alterar e reiniciar api/worker |
| Executor remoto indisponível | Ver PB-06 (worker unhealthy) |
| MEXC API error (rate limit / invalid key) | Verificar chaves `MEXC_API_KEY`/`MEXC_API_SECRET`; rotacionar se necessário |

### Gate de segurança atual

O sistema tem **múltiplas camadas** antes de criar uma ordem real:
1. `LOBO_MIRROR_ORIGIN_SIMULATION_ONLY=false` (OK — real habilitado)
2. `EXCHANGE_SANDBOX=true` (BLOQUEIO — vai para sandbox MEXC)
3. `CRYPTO_REAL_ORDER_ENABLED=false` (BLOQUEIO adicional)
4. `Saldo zero` (BLOQUEIO operacional)

Para produção real: remover camadas 2 e 3, garantir saldo em camada 4.

### Rollback de posição real aberta sem SL

```bash
# Ativar kill switch via API
curl -X POST 'http://127.0.0.1:8002/api/v1/crypto/kill-switch/activate' \
  -H "X-API-Key: $API_KEY" \
  -d '{"reason":"SL_NAO_CRIADO"}'

# Fechar posição manualmente via MEXC UI
# Depois reconciliar no DB:
curl -X POST "http://127.0.0.1:8002/api/v1/lobo-mirror/origin/trades/<trade_id>/close" \
  -H "X-API-Key: $API_KEY" -d '{"close_reason":"manual_emergency"}'
```

---

## PB-04 — Redis Indisponível

**Sintoma:** API retorna 500 em rotas que usam cache/queue; `redis: error` no `/ready`.

### Diagnóstico

```bash
# 1. Status dos containers Redis
docker ps --filter 'name=redis' --format 'table {{.Names}}\t{{.Status}}'

# 2. Testar conectividade
REDIS_PASS=$(docker inspect multi_project_infra-redis-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep REDIS_PASSWORD= | cut -d= -f2-)
docker exec multi_project_infra-redis-1 redis-cli -a "$REDIS_PASS" ping

# poupi-crypto redis (tem senha desde 2026-06-13)
CRYPTO_PASS=$(cat /opt/apps/poupi-crypto/.env | grep CRYPTO_REDIS_PASSWORD= | cut -d= -f2-)
docker exec poupi-crypto-redis-1 redis-cli -a "$CRYPTO_PASS" ping

# 3. Verificar uso de memória
docker exec multi_project_infra-redis-1 redis-cli -a "$REDIS_PASS" INFO memory | grep used_memory_human
```

### Redis por serviço

| Redis | Serviço | DB | Senha |
|---|---|---|---|
| `multi_project_infra-redis-1` | poupi-baby (BullMQ), data-core | db0, db2 | `REDIS_PASSWORD` em infra/.env |
| `poupi-crypto-redis-1` | poupi-crypto api+worker | db0 | `CRYPTO_REDIS_PASSWORD` em /opt/apps/poupi-crypto/.env |
| `poupi-crypto-volatile-redis-1` | volatile-api | db0 | sem senha (isolada) |

### Ação por causa

| Causa | Ação |
|---|---|
| Container stopped | `docker start <redis-container>` |
| OOM (maxmemory atingido) | `redis-cli CONFIG SET maxmemory-policy allkeys-lru` |
| Dados corrompidos (AOF) | Remover `/data/appendonly.aof`; restart (PERDE dados) |
| Volume inacessível | `docker volume inspect <vol>`; verificar disco cheio |

### Restart com preservação de dados

```bash
# shared redis (appendonly não habilitado — sem persistência)
docker restart multi_project_infra-redis-1

# crypto redis (appendonly yes — dados preservados no volume)
cd /opt/apps/poupi-crypto
docker compose restart redis
# aguardar redis saudável antes de reiniciar api/worker:
docker compose up -d --no-deps api worker
```

---

## PB-05 — MEXC Indisponível

**Sintoma:** Logs com `httpx.ConnectError` ou `TimeoutError` para `contract.mexc.com` / executor remoto.

### Diagnóstico

```bash
# 1. Testar MEXC public API
curl -fsS --max-time 10 'https://contract.mexc.com/api/v1/contract/detail?symbol=BTC_USDT' | head -100

# 2. Testar executor remoto
EXEC_URL=$(docker inspect poupi-crypto-worker-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep MEXC_REMOTE_EXECUTOR_URL= | cut -d= -f2-)
curl -fsS --max-time 10 "$EXEC_URL/balance"

# 3. Verificar kill switch
curl -fsS http://127.0.0.1:8002/ready | python3 -m json.tool | grep -A3 'kill_switch'

# 4. Logs do worker
docker logs poupi-crypto-worker-1 --tail 50 | grep -E 'MEXC|ConnectError|timeout|Timeout'
```

### Comportamento esperado durante outage

O sistema **já tem proteção automática:**
- `Saldo zero → conta cav pausada por 60 min` — novos sinais ignorados
- Kill switch por drawdown protege posições abertas
- Worker continua monitorando; sinais do canal Lobo Mirror ficam em buffer

### Ação

| Causa | Ação |
|---|---|
| Outage MEXC (HTTP 5xx) | Aguardar retorno; sem ação manual necessária |
| Executor remoto offline (Cloudflare tunnel) | Verificar serviço no host do executor; reiniciar tunnel |
| DNS resolution falha | Verificar `/etc/resolv.conf` no VPS; reiniciar `systemd-resolved` |
| Rate limit (HTTP 429) | Reduzir frequência de calls; aguardar cooldown |

### Posições abertas durante outage MEXC

```bash
# Verificar posições sem SL confirmado
API_KEY=$(docker inspect poupi-crypto-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^API_KEY=' | cut -d= -f2-)
curl -fsS 'http://127.0.0.1:8002/ready' | python3 -m json.tool | grep unprotected_positions

# Se unprotected_positions > 0: ativar kill switch
curl -X POST 'http://127.0.0.1:8002/api/v1/crypto/kill-switch/activate' \
  -H "X-API-Key: $API_KEY" -d '{"reason":"MEXC_INDISPONIVEL"}'
```

---

## PB-06 — Worker Unhealthy

**Sintoma:** `Health=unhealthy` no `docker ps` para `poupi-crypto-worker-1`; canal Lobo Mirror sem processamento.

### Diagnóstico

```bash
# 1. Status detalhado
docker inspect poupi-crypto-worker-1 --format '{{json .State.Health}}' | python3 -m json.tool

# 2. Logs do worker
docker logs poupi-crypto-worker-1 --tail 100 2>&1

# 3. Verificar conexão Telethon (MTProto)
docker logs poupi-crypto-worker-1 2>&1 | grep -E 'telethon|MTProto|Connecting|Connection' | tail -10

# 4. Verificar Redis (worker usa REDIS_URL)
CRYPTO_PASS=$(cat /opt/apps/poupi-crypto/.env | grep CRYPTO_REDIS_PASSWORD= | cut -d= -f2-)
docker exec poupi-crypto-redis-1 redis-cli -a "$CRYPTO_PASS" ping
```

### Causas e ações

| Causa | Ação |
|---|---|
| Sessão Telethon expirada | Ver abaixo — renovar sessão |
| Redis indisponível | Ver PB-04 |
| OOM (256MB limit) | Verificar `docker stats`; reiniciar worker |
| Erro não tratado (Traceback) | `docker logs` → corrigir código → redeploy |
| Network partition (infra_internal) | `docker network inspect infra_internal` |

### Renovar sessão Telethon

```bash
# Localizar arquivo de sessão
docker exec poupi-crypto-worker-1 ls /app/sessions/

# Opção 1: reiniciar worker (tenta reconectar automaticamente)
cd /opt/apps/poupi-crypto
docker compose restart worker

# Opção 2: regenerar sessão (requer interação manual)
# 1. Parar worker
docker compose stop worker
# 2. Executar script de geração de sessão localmente
# 3. Copiar nova sessão para o VPS
# 4. Reiniciar worker
docker compose up -d --no-deps worker
```

### Sinais perdidos durante unhealthy

O Telethon não faz buffer de mensagens históricas — sinais recebidos enquanto o worker estava unhealthy são **perdidos**. Verificar canal Lobo Mirror manualmente após recovery se houver gap de tempo.

---

## Referência Rápida — Comandos Diagnóstico

```bash
# Status geral de todos os containers Poupi
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | grep -E 'poupi|data.core|infra|volatile'

# RAM de todos os containers
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'

# Saúde do ecossistema crypto
curl -fsS http://127.0.0.1:8002/ready | python3 -m json.tool

# Saúde do data-core
curl -fsS http://127.0.0.1:8000/health | python3 -m json.tool

# Kill switch status
API_KEY=$(cat /opt/apps/poupi-crypto/.env | grep '^API_KEY=' | cut -d= -f2-)
curl -fsS http://127.0.0.1:8002/ready -H "X-API-Key: $API_KEY" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get("origin_real",{}), indent=2))'

# Backup manual (ghost DBs já removidos — apenas DBs ativos)
docker exec multi_project_infra-postgres-1 pg_dump -U postgres -Fc data_core_db > /tmp/data_core_$(date +%Y%m%d).dump
docker exec multi_project_infra-postgres-1 pg_dump -U postgres -Fc poupi_baby_db > /tmp/poupi_baby_$(date +%Y%m%d).dump
```

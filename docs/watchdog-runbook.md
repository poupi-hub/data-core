# Watchdog Runbook — Ausência do Operador

> Guia de diagnóstico e ação para cada alerta do watchdog.
> Válido para operações remotas e períodos sem monitoramento ativo.

---

## Checklist rápido ao receber alerta

1. Verificar `/api/v1/watchdog/status` → ver status geral e alertas
2. Identificar o código do alerta (campo `code`)
3. Navegar para a seção correspondente abaixo
4. Executar a query de diagnóstico SQL
5. Tomar a ação corretiva

---

## collection_stale — Coleta parada por domínio

**O que significa**: Uma fonte específica (ex: `drogasil`) não gerou nenhum novo registro em
`raw_collections` nas últimas N horas.

**Diagnóstico SQL**:
```sql
-- Última coleta por fonte
SELECT
    source_name,
    MAX(collected_at) AS last_at,
    EXTRACT(EPOCH FROM (NOW() - MAX(collected_at)))/3600 AS age_hours
FROM raw_collections
WHERE source_name IN ('drogasil', 'drogaraia', 'paguemenos')
GROUP BY source_name
ORDER BY last_at DESC;
```

**Causas possíveis**:
1. Scheduler parou — verificar logs do processo FastAPI
2. Target desativado — verificar `collection_targets WHERE active=false`
3. Site bloqueou o scraper — verificar `metadata_json->>'anti_bot_detected'`
4. Mudança de URL ou estrutura do site (drift)

**Ações**:
```bash
# 1. Verificar se scheduler está rodando
curl -s http://localhost:8000/ready | jq '.checks.scheduler'

# 2. Ver targets ativos
psql -c "SELECT id, url, active FROM collection_targets WHERE source_name='drogasil';"

# 3. Forçar uma coleta manual
python -c "
from collectors.ecommerce.url_scraper import EcommerceURLScraper
from database.session import SessionLocal
db = SessionLocal()
scraper = EcommerceURLScraper()
result = scraper.collect_targets(db)
print(result)
db.close()
"
```

---

## collection_platform_down — Nenhuma coleta de qualquer fonte

**O que significa**: NENHUMA fonte coletou dados nas últimas N horas. Indica falha sistêmica.

**Diagnóstico SQL**:
```sql
SELECT MAX(collected_at) AS last_any, NOW() - MAX(collected_at) AS age
FROM raw_collections;
```

**Causas prováveis**:
1. Scheduler não está rodando (`/ready` retorna `scheduler: not running`)
2. Banco de dados indisponível
3. Processo da aplicação caiu

**Ações**:
```bash
# 1. Verificar saúde geral
curl http://localhost:8000/health

# 2. Reiniciar aplicação (Coolify / Docker)
# Via Coolify: ir em Services → data-core → Restart

# 3. Verificar logs da aplicação
docker logs data-core-app --tail 200
```

---

## normalization_backlog — Fila de normalização acumulando

**O que significa**: Registros raw estão presos em `normalization_pending` por mais de 45 min.

**Diagnóstico SQL**:
```sql
-- Registros stuck
SELECT
    source_name,
    COUNT(*) AS pending_count,
    MIN(collected_at) AS oldest_pending,
    EXTRACT(EPOCH FROM (NOW() - MIN(collected_at)))/60 AS age_minutes
FROM raw_collections
WHERE processing_status = 'normalization_pending'
GROUP BY source_name
ORDER BY oldest_pending;

-- Últimos erros de normalização
SELECT
    source_name,
    collected_at,
    metadata_json->>'error' AS error
FROM raw_collections
WHERE processing_status = 'normalization_failed'
ORDER BY collected_at DESC
LIMIT 10;
```

**Causas prováveis**:
1. `normalize_job` falhou silenciosamente
2. Erro de schema no normalizer para um tipo específico de payload
3. Dependência externa (ex: enriquecimento de dados) indisponível

**Ações**:
```bash
# 1. Verificar última execução do normalize_job
# Checar logs com: grep "normalize_job" app.log | tail -20

# 2. Forçar normalização manual
python -c "
from scheduler.jobs import normalize_job
normalize_job()
"

# 3. Ver payload problemático
psql -c "
SELECT id, source_name, payload_json, collected_at
FROM raw_collections
WHERE processing_status='normalization_pending'
ORDER BY collected_at ASC
LIMIT 5;
"
```

---

## normalization_low_success_rate — Baixa taxa de normalização

**O que significa**: Fonte com menos de 70% de registros normalizados com sucesso nas últimas 24h.

**Diagnóstico SQL**:
```sql
SELECT
    source_name,
    processing_status,
    COUNT(*) AS cnt,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY source_name), 1) AS pct
FROM raw_collections
WHERE collected_at >= NOW() - INTERVAL '24 hours'
GROUP BY source_name, processing_status
ORDER BY source_name, processing_status;
```

**Ação**: Ver seção `normalization_backlog` para diagnóstico do normalizer.

---

## normalization_stale — Última normalização antiga

**O que significa**: Nenhum produto foi normalizado nas últimas 4h.

**Diagnóstico SQL**:
```sql
SELECT MAX(normalized_at) AS last_normalized, NOW() - MAX(normalized_at) AS age
FROM normalized_products;
```

**Ação**: Forçar `normalize_job()` manualmente (ver `normalization_backlog`).

---

## scraper_quality_low — Qualidade de payload baixa

**O que significa**: Fonte com score médio <50/100 — payloads sem dados essenciais (preço, título, disponibilidade).

**Diagnóstico SQL**:
```sql
SELECT
    source_name,
    AVG((metadata_json->'quality'->>'score')::float) AS avg_score,
    MIN((metadata_json->'quality'->>'score')::float) AS min_score,
    COUNT(*) AS samples
FROM raw_collections
WHERE collected_at >= NOW() - INTERVAL '24 hours'
  AND metadata_json IS NOT NULL
  AND metadata_json->'quality' IS NOT NULL
GROUP BY source_name
ORDER BY avg_score;
```

**Causas prováveis**:
1. Estrutura da página mudou (JSON-LD removido, VTEX API mudou)
2. Anti-bot retornando página vazia/captcha
3. SKU descontinuado (sem preço)

**Ação**: Ver drift events abertos para a fonte:
```sql
SELECT drift_type, risk_level, detected_at, field_name
FROM scraper_drift_events
WHERE source_name = 'drogasil'
  AND resolved_at IS NULL
ORDER BY detected_at DESC;
```

---

## anti_bot_spike — Bloqueio anti-bot crescendo

**O que significa**: Fonte com ≥3 detecções de anti-bot na última hora.

**Diagnóstico SQL**:
```sql
SELECT
    source_name,
    COUNT(*) AS detections,
    MAX(collected_at) AS last_detection
FROM raw_collections
WHERE collected_at >= NOW() - INTERVAL '1 hour'
  AND metadata_json->>'anti_bot_detected' = 'true'
GROUP BY source_name
ORDER BY detections DESC;
```

**Ações imediatas**:
1. Aumentar interval dos jobs de coleta (reduzir frequência)
2. Verificar se o IP foi banido — testar manualmente
3. Considerar usar proxy rotation (não implementado — ticket futuro)

---

## scraper_drift_detected — Mudança estrutural no site

**O que significa**: Campos essenciais ausentes ou tipos mudados no payload recente vs. baseline.

**Resolver via API**:
```bash
# Ver drift events abertos
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/scrapers/drift?unresolved_only=true

# Após corrigir o scraper, marcar como resolvido
curl -X POST -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/scrapers/drift/resolve/{event_id}
```

---

## telegram_publish_failing — Falha no envio Telegram

**O que significa**: poupi-baby está reportando falhas no envio para o bot Telegram.

**Diagnóstico**:
```sql
-- Ver falhas recentes
SELECT status, fail_reason, published_at, marketplace
FROM telegram_publication_events
WHERE published_at >= NOW() - INTERVAL '6 hours'
ORDER BY published_at DESC;
```

**Causas**:
1. Bot token inválido ou revogado
2. Chat ID incorreto
3. Bot foi removido do grupo
4. Rate limit do Telegram (429)

**Verificar bot**:
```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
```

---

## telegram_no_publication_products_exist — Sem publicação mas produtos existem

**O que significa**: Há produtos normalizados mas nenhuma publicação Telegram. Possivelmente deal_score abaixo do mínimo configurado no poupi-baby.

**Diagnóstico**:
```sql
-- Verificar ofertas recentes e seus deal_scores
SELECT marketplace, price, normalized_at
FROM normalized_products
WHERE normalized_at >= NOW() - INTERVAL '6 hours'
ORDER BY normalized_at DESC
LIMIT 20;
```

**Ação no poupi-baby**: Verificar threshold de `deal_score` em `TelegramGroupProcessor` — pode estar muito alto.

---

## Comandos de diagnóstico rápido

```bash
# Status geral do watchdog
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/watchdog/status | jq .

# Forçar heartbeat (envia resumo ao Telegram)
curl -X POST -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/watchdog/heartbeat/send

# Últimas 5 runs do watchdog
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/api/v1/watchdog/runs?limit=5" | jq '.[] | {run_at, overall_status, alert_codes}'

# Saúde do sistema
curl http://localhost:8000/health | jq .
curl http://localhost:8000/ready | jq .

# Métricas Prometheus (texto)
curl http://localhost:8000/metrics | grep watchdog
```

# Data Core Collection Readiness

Data: 2026-05-31

## Endpoints

Comandos:

```bash
curl http://10.0.1.2:8000/live
curl http://10.0.1.2:8000/health
curl http://10.0.1.2:8000/ready
```

Resultados:

```text
/live  -> {"status":"alive","app":"data-core"}
/health -> degraded
/ready -> timeout em 5s
```

Detalhe de `/health`:

```text
postgres = ok
redis = error
detail = max requests limit exceeded. Limit: 500000, Usage: 500000
```

## Banco

Tabelas confirmadas:

```text
collection_runs
collector_definitions
collector_errors
raw_collections
```

Alembic observado antes da alteracao manual:

```text
alembic_version = 0020_trading_signal_outcomes
```

Alteracao aplicada:

```text
collectordomain.jobs = ensured
```

Motivo: `CollectionRun.domain` e `CollectorDefinition.domain` usam enum `collectordomain`; coletores Jobs falhariam sem o valor `jobs`.

## Scheduler / Worker

Containers observados healthy:

```text
data-core api
data-core scheduler
data-core worker
```

Logs do scheduler mostraram jobs de ecommerce falhando com:

```text
sqlalchemy.exc.PendingRollbackError
SSL connection has been closed unexpectedly
```

Correção aplicada em hot patch:

```text
workers/collector_worker.py
```

O worker agora faz `db.rollback()` apos `db.refresh(run)`, evitando transacao aberta durante coletores longos.

## Raw Persistence

Antes do hot patch, `raw_collections` no servidor:

```text
crypto = 34652 registros recentes
real_estate/apolar legado = 133 registros
real_estate/direct_agencies = 0 registros
jobs = 0 registros
```

## Veredito

**PARTIAL / DEGRADED**

Data-core API esta viva e Postgres conecta, mas:

- Redis configurado esta degradado por limite externo.
- `/ready` nao respondeu.
- servidor data-core estava com imagem defasada.
- Real Estate e Jobs prioritarios nao estavam operacionais de ponta a ponta no servidor.


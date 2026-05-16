# Phase A Report — Consolidação do data-core

> Gerado em: 2026-05-16. Tipo: relatório de fase.
> Escopo: data-core (Hetzner / Coolify). Commits: `bf568ec` → `35ab1a0`.

---

## O que foi implementado

### Observabilidade (Prometheus)
| Arquivo | Mudança |
|---|---|
| `api/metrics.py` | +14 métricas: `pipeline_stage_*`, `collection_*`, `db_pool_*` |
| `api/metrics.py` | `measure_pipeline_stage(domain, stage)` context manager |
| `scheduler/jobs.py` | `normalize_job` + `analytics_job` wrapped com `measure_pipeline_stage` via `PipelineRecorder` |

### Correlação e Logging Estruturado
| Arquivo | Mudança |
|---|---|
| `app/middleware/correlation.py` | **NOVO** — `CorrelationMiddleware`: gera `X-Correlation-ID` + `X-Trace-ID` por request |
| `app/middleware/__init__.py` | **NOVO** |
| `logs/config.py` | `CorrelationFilter` + `PipelineFilter` + `set_pipeline_context()` + JSON logging (`LOG_JSON=true`) |
| `app/main.py` | `CorrelationMiddleware` registrado |

### Pipeline History (DB)
| Arquivo | Mudança |
|---|---|
| `app/pipeline/models.py` | **NOVO** — `PipelineRun` + `PipelineFailure` SQLAlchemy models |
| `app/pipeline/__init__.py` | **NOVO** |
| `app/pipeline/recorder.py` | **NOVO** — `PipelineRecorder` context manager |
| `alembic/versions/0015_pipeline_observability.py` | **NOVO** — migration: cria `pipeline_runs` + `pipeline_failures` |
| `scheduler/jobs.py` | `normalize_job` + `analytics_job` wrapped com `PipelineRecorder` |

### Health Probes
| Arquivo | Mudança |
|---|---|
| `app/main.py` | `GET /live` — liveness probe (sempre 200, sem DB) |
| `app/main.py` | `GET /ready` — readiness probe (postgres + redis + scheduler, retorna 503 se não pronto) |

### Grafana
| Arquivo | Mudança |
|---|---|
| `docs/grafana-dashboard-data-core-ops.json` | **NOVO** — dashboard operacional `data-core-ops-v1` |
| Produção | Dashboard importado em `http://10.0.2.7:3000` (UID: `data-core-ops-v1`) |

### Documentação `/docs`
| Arquivo | Status |
|---|---|
| `docs/DATA_FLOW.md` | **NOVO** — ETL flow por domínio, stage details, timing |
| `docs/JOBS_AND_SCHEDULES.md` | **NOVO** — todos os jobs, triggers, reliability |
| `docs/API_ENDPOINTS.md` | **NOVO** — todos os endpoints REST com exemplos |
| `docs/OBSERVABILITY.md` | **NOVO** — métricas, alertas, logs, health checks, SQL queries |
| `docs/AUDIT.md` | **NOVO** — auditoria completa, gaps, priority matrix |
| `docs/AI_CONTEXT.md` | Atualizado — redireciona para `ai/CONTEXT.md` |

### Documentação `/ai`
| Arquivo | Status |
|---|---|
| `ai/CONTEXT.md` | **NOVO** — contexto operacional completo para agentes |
| `ai/RUNBOOK.md` | **NOVO** — playbook: diagnosticar, deployar, ativar domínio |
| `ai/DOC_SYNC_RULES.md` | **NOVO** → atualizado com regras canônicas |
| `ai/reports/PHASE_A_REPORT.md` | **NOVO** — este arquivo |

### READMEs
| Arquivo | Mudança |
|---|---|
| `README.md` | Reescrito — arquitetura atual, 3 containers, endpoints corretos, migration head |
| `AGENTS.md` | Atualizado — Phase A components, doc sync rule, domain status |

---

## O que foi deployado em produção

| Item | Evidência |
|---|---|
| Containers rodando com commit Phase A | `docker ps` → imagem `503fddf...` |
| Migration `0015_pipeline_observability` aplicada | `alembic current` → `0015_pipeline_observability (head)` |
| `GET /live` → 200 | `{"status":"alive","app":"data-core"}` |
| `GET /ready` → 200 | `{"ready":true,"checks":{"postgres":"ok","redis":"ok"}}` |
| `pipeline_runs` e `pipeline_failures` criadas | Query: 10 runs gravados em primeira execução |
| Grafana dashboard importado | UID `data-core-ops-v1` ativo em produção |
| Alias `data-core-api` na rede coolify mantido | Poupi-crypto continua conectado |

---

## Documentação sincronizada

| Arquivo | Status |
|---|---|
| `docs/DATA_FLOW.md` | ✅ Sincronizado |
| `docs/JOBS_AND_SCHEDULES.md` | ✅ Sincronizado |
| `docs/API_ENDPOINTS.md` | ✅ Sincronizado |
| `docs/OBSERVABILITY.md` | ✅ Sincronizado |
| `docs/AUDIT.md` | ✅ Sincronizado |
| `ai/CONTEXT.md` | ✅ Sincronizado |
| `ai/RUNBOOK.md` | ✅ Sincronizado |
| `ai/DOC_SYNC_RULES.md` | ✅ Atualizado com regras canônicas |
| `README.md` | ✅ Reescrito |
| `AGENTS.md` | ✅ Atualizado |
| `Projetos/docs/projects-summary.md` | ✅ Seção data-core atualizada |
| `Projetos/docs/documentation_rules.md` | ✅ Criado (fonte canônica) |

---

## Gaps pendentes (herdados do audit)

| P | Item | Arquivo de referência |
|---|---|---|
| P1 | `collection_raw_saved_total` não incrementado em `collector_worker.py` | `docs/AUDIT.md` §2C |
| P1 | Ecommerce: sem dados reais (seed `collection_targets`) | `docs/AUDIT.md` §2A |
| P1 | Sports: `THE_ODDS_API_KEY` não configurado | `docs/AUDIT.md` §2A |
| P2 | `LOG_JSON=true` não ativo nos containers | `docs/AUDIT.md` §2E |
| P2 | Redis cache desabilitado (`CACHE_ENABLED=false`) | `docs/AUDIT.md` §2F |
| P2 | Alertas de staleness não adicionados às Prometheus rules | `docs/AUDIT.md` §2C |
| P2 | Credentials hardcoded no docker-compose.yml | `docs/AUDIT.md` §2J |
| P3 | Prometheus multi-processo: worker não expõe `/metrics` | `ai/CONTEXT.md` §Observability |
| P3 | Diagramas Mermaid ausentes em `docs/DATA_FLOW.md` | Pendente |

---

## Próximos passos recomendados

1. **Phase B — Ativar domínios** (P1):
   - Seed `collection_targets` com URLs reais de ecommerce
   - Configurar `THE_ODDS_API_KEY` via Coolify
   - Registrar `ApolarCollector` no scheduler

2. **Phase B — Métricas e alertas** (P2):
   - Incrementar `collection_raw_saved_total` em `collector_worker.py`
   - Adicionar regras de staleness em `prometheus/rules/data-core-alerts.yml`
   - Habilitar `LOG_JSON=true` via Coolify env vars

3. **Manutenção** (P1):
   - `docker buildx prune` no servidor (liberar 4.9GB)
   - Substituir credenciais hardcoded no `docker-compose.yml` por `${VAR}`

4. **Diagramas** (próxima fase):
   - Converter diagramas ASCII em Mermaid nos docs

---

## Commits desta fase

```
35ab1a0  docs: create /ai directory and sync all documentation to Phase A state
503fddf  Merge branch 'main' of https://github.com/poupi-hub/data-core
bf568ec  feat(observability): Phase A — pipeline observability, health probes, structured logging
```

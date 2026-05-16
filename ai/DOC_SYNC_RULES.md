# Documentation Sync Rules — data-core

> Fonte canônica (plataforma inteira): `Projetos/docs/documentation_rules.md`
> Este arquivo contém o resumo aplicado ao data-core + referência às regras globais.

---

## Regra global

Qualquer mudança em arquitetura, pipeline, endpoint, schema, job, collector, observabilidade,
deployment ou variável de ambiente → atualizar **automaticamente, sem instrução explícita**:

1. `/docs/*.md` relevante
2. `ai/CONTEXT.md` (topology, key files, known gaps)
3. `README.md` (se setup, endpoints ou roles mudaram)
4. `AGENTS.md` (se regras de código mudaram)
5. Gerar relatório em `ai/reports/` ou `docs/reports/`

---

## Gatilhos de sincronização — data-core

| Mudança | Arquivos a atualizar |
|---|---|
| Novo endpoint | `docs/API_ENDPOINTS.md`, `ai/CONTEXT.md` §API endpoints, `README.md` |
| Nova migration | `README.md` §Adding a migration (novo head), `ai/CONTEXT.md` §Database, `AGENTS.md` |
| Novo collector | `docs/DATA_FLOW.md`, `docs/JOBS_AND_SCHEDULES.md`, `ai/CONTEXT.md` |
| Novo job/schedule | `docs/JOBS_AND_SCHEDULES.md`, `ai/CONTEXT.md` §Jobs |
| Novo metric Prometheus | `docs/OBSERVABILITY.md`, `ai/CONTEXT.md` §Observability |
| Domínio ativado | `docs/DATA_FLOW.md`, `ai/CONTEXT.md` §Known gaps, `docs/AUDIT.md`, `docs/projects-summary.md` |
| Novo container/env var | `README.md`, `ai/CONTEXT.md` §Runtime topology, `AGENTS.md` |
| Schema DB mudou | `docs/DATA_FLOW.md`, `ai/CONTEXT.md` §Database |
| Alert rule adicionada | `docs/OBSERVABILITY.md` |
| Gap fechado | `docs/AUDIT.md` §Priority matrix, `ai/CONTEXT.md` §Known gaps |
| Deploy procedure mudou | `ai/RUNBOOK.md` §Deploy, `ai/CONTEXT.md` §Deployment |

---

## Regra de diagramas

Toda mudança em fluxo ETL ou arquitetura deve atualizar o diagrama Mermaid em `docs/DATA_FLOW.md`.

Toda mudança em topologia de containers deve atualizar o diagrama ASCII/Mermaid em `README.md`.

---

## Regra de relatórios

Toda fase operacional gera um relatório em `ai/reports/PHASE_<X>_REPORT.md` contendo:

* o que foi implementado;
* o que foi sincronizado na documentação;
* gaps restantes;
* próximos passos.

---

## Anti-duplicação

| Conteúdo | Fonte única | Outros arquivos |
|---|---|---|
| Endpoints completos | `docs/API_ENDPOINTS.md` | `ai/CONTEXT.md` → referencia |
| ETL flow detalhado | `docs/DATA_FLOW.md` | `ai/CONTEXT.md` → resume |
| Jobs e schedules | `docs/JOBS_AND_SCHEDULES.md` | `ai/CONTEXT.md` → resume |
| Metrics referência | `docs/OBSERVABILITY.md` | `ai/CONTEXT.md` → lista apenas nomes |
| Topology runtime | `ai/CONTEXT.md` | `README.md` → diagrama visual |
| Gaps e prioridades | `docs/AUDIT.md` | `ai/CONTEXT.md` → tabela resumida |
| Regras globais de doc | `Projetos/docs/documentation_rules.md` | Este arquivo → resume |

---

## Checklist de end-of-phase

```
[ ] /docs/*.md descreve a realidade atual (não o passado)
[ ] ai/CONTEXT.md §Known gaps está atualizado
[ ] ai/CONTEXT.md §Runtime topology reflete containers atuais
[ ] README.md quick-start funciona
[ ] AGENTS.md regras batem com a implementação
[ ] Nenhum arquivo referencia container names antigos ou endpoints deletados
[ ] Exemplos nos docs podem ser executados em produção
[ ] Relatório de fase gerado em ai/reports/
[ ] Diagramas Mermaid atualizados
```

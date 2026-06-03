# Crypto + Data Core Alerting Refactor

Data: 2026-05-30

## Objetivo

Reduzir ruido e tornar alertas Telegram orientados a acao, sem alterar estrategia,
indicadores, thresholds de trading, enforcement ou DRY_RUN.

## Padrao Telegram

Todos os alertas enviados por Alertmanager passam a usar o template:

```text
ALERTA

Sistema:
<sistema/componente/runtime>

Problema:
<o que aconteceu>

Impacto:
<o que pode acontecer>

Urgencia:
BAIXA / MEDIA / ALTA / CRITICA

Acao:
<o que verificar>

Evidencia:
<metrica, valor, instancia ou timestamp>

Dashboard:
<dashboard recomendado>
```

O template aceita anotacoes estruturadas (`system`, `problem`, `impact`,
`urgency`, `action`, `evidence`, `dashboard`) e usa fallback para
`summary`/`description` quando uma rule antiga ainda nao foi enriquecida.

## Inventario Consolidado

| Alerta / familia | Origem | Trigger | Cooldown | Severidade | Canal logico | Decisao |
|---|---|---:|---:|---|---|---|
| DataCoreApiDown / Shadow DataCore API Down | Prometheus / Alertmanager | API indisponivel | 2h critical | critical | CRITICAL OPERATIONS | Manter e ajustar |
| PostgresDown / PoupiPostgresDown | Prometheus / Alertmanager | Postgres indisponivel | 2h critical | critical | CRITICAL OPERATIONS | Manter e ajustar |
| RedisDown / PoupiRedisDown / VolatileRedisDown | Prometheus / Alertmanager | Redis indisponivel | 2h critical, 6h warning | critical/warning | CRITICAL OPERATIONS / OPERATIONS | Manter e deduplicar |
| SchedulerHeartbeatMissing / Dead | Data Core pipeline rules | heartbeat ausente | 2h critical | critical | CRITICAL OPERATIONS | Manter e ajustar |
| SchedulerConsecutiveFailures / ExecutionDrift | Data Core pipeline rules | falhas/drift sustentados | 6h warning | warning | OPERATIONS | Manter |
| PipelineDead / Blocked / Stalled | Data Core pipeline rules | pipeline parado/bloqueado | 2h/6h | critical/warning | CRITICAL OPERATIONS / OPERATIONS | Manter |
| QueueBacklogExploding / Elevated / OldestJobAgeCritical | Data Core pipeline rules | backlog ou idade alta | 2h/6h | critical/warning | OPERATIONS | Manter e inibir por causa raiz |
| NormalizationLagCritical / AnalyticsLagCritical | Data Core pipeline rules | lag alto | 2h critical | critical | CRITICAL OPERATIONS | Manter |
| Data Core scheduler memory/swap/OOM/restart/leak/backlog/state | Data Core scheduler rules | pressao de runtime | 2h/6h | critical/warning | CRITICAL OPERATIONS / OPERATIONS | Manter e ajustar |
| Enforcement policy fetch/fallback/cache/kill switch/blocking/mode/confidence | Enforcement rules | enforcement degradado | 2h/6h | critical/warning | CRITICAL OPERATIONS | Manter e ajustar |
| OutcomePipelineStalled / Warning | Outcome pipeline rules | health score baixo sem bootstrap | 2h/6h | critical/warning | RESEARCH / OPERATIONS | Manter e ajustar |
| OutcomePendingBacklogHigh | Outcome pipeline rules | pending outcomes > 200 | 6h | warning | RESEARCH | Manter |
| OutcomeJobNotRunning | Outcome pipeline rules | lag > 2h com pendencia | 2h | critical | CRITICAL OPERATIONS | Manter |
| OutcomeEvalErrorSpike / FutureCandlesMissing | Outcome pipeline rules | erro/candles ausentes | 6h | warning | RESEARCH | Manter |
| OutcomeLowAccuracy | Outcome pipeline rules | accuracy < 35% | 6h | warning | RESEARCH | Manter, advisory |
| TelegramDirectFailure | Poupi Crypto metrics + rules | falha publisher direto em 15m | 6h | warning | OPERATIONS | Novo |
| HOST_SWAP_CRITICAL | Poupi Crypto observability | swap > 90% | 2h | critical | CRITICAL OPERATIONS | Ajustado |
| RuntimeWatchdogUnhealthy | Poupi Crypto observability | watchdog state >= 3 | 2h | critical | CRITICAL OPERATIONS | Ajustado |
| DatasetWriteFailure / ReplaySnapshotStale / SignalTelemetrySilent | Poupi Crypto observability | persistencia/replay ausente | 6h | warning | OPERATIONS | Ajustado |
| PaperNoUsefulSample / ShadowDeltaExtreme / ReplayConsistencyFailure | Poupi Crypto observability | amostra/replay divergente | 6h | warning | RESEARCH | Ajustado |
| Edge Detected | Research publisher direto | PF >= 1.15 e expectancy > 0, N >= 50 | 12h | info/media | RESEARCH | Ajustado |
| Edge Deterioration | Research publisher direto | queda PF >= 20% ou WR >= 10pp, N >= 50 | 6h | warning | RESEARCH | Ajustado |
| Confidence Drift | Research publisher direto | high/low confidence fora do esperado | 12h | warning | RESEARCH | Ajustar gradual |
| Bot Divergence | Research publisher direto | spread WR >= 20pp | 6h | warning | RESEARCH | Ajustar gradual |
| Regime Change | Research publisher direto | dominante muda ou cai >= 25pp | 6h | info | RESEARCH | Ajustar gradual |
| No Signals | Research publisher direto | bot sem decisoes > 4h | 2h | warning | RESEARCH | Manter, inibir por scheduler |
| Dataset Quality | Research publisher direto | coverage < 60% | 12h | warning | RESEARCH | Manter |
| Outcome Backlog | Research publisher direto | pending mature > 500 | 4h | warning | RESEARCH | Manter, inibir por scheduler |
| Regime Coverage | Research publisher direto | regime com N < 20 | 12h | info | RESEARCH | Manter |
| Statistical Significance | Research publisher direto | z > 1.645, N >= 50 | 24h | info | RESEARCH | Ajustado |
| Research Layer Stalled | Research publisher direto | decisoes/outcomes parados > 8h | 4h | alta | RESEARCH / OPERATIONS | Novo |
| Research Decisions Not Growing | Research publisher direto | zero decisoes em 24h | 6h | alta | RESEARCH / OPERATIONS | Novo |
| Research Outcomes Not Growing | Research publisher direto | zero outcomes em 24h com pendencia madura | 6h | alta | RESEARCH / OPERATIONS | Novo |
| Acceptance Rate Anomaly | Research publisher direto | 24h < 2% ou > 80%, N >= 50 | 6h | media | RESEARCH | Novo |
| Regime Missing | Research publisher direto | regime vazio >= 20%, N >= 30 | 6h | media | RESEARCH | Novo |

## Alertas Removidos ou Silenciados

| Alerta | Motivo |
|---|---|
| OutcomePipelineBootstrapActive | Bootstrap sem acao operacional direta |
| DatasetStillBootstrapping | Bootstrap/maturidade gera ruido |
| DatasetMaturityUseful | Maturidade informativa, sem urgencia |
| DatasetCalibrationReady | "Ready" gera falsa conclusao/prematuridade |
| VolatileDatasetBootstrapping | Bootstrap do runtime volatile, sem acao pratica |

## Research Mode

Regras de amostra para edge:

| N | Tratamento |
|---:|---|
| N < 50 | Nao emitir edge |
| 50 <= N < 100 | PRELIMINARY |
| 100 <= N < 250 | PREFERRED |
| N >= 250 | STRONG_EVIDENCE |

Todos os alertas de edge deixam explicito que sao advisory-only e nao alteram
estrategia, thresholds, enforcement ou DRY_RUN.

## Deduplicacao por Causa Raiz

Inibicoes implementadas no Alertmanager versionado:

| Causa raiz | Alertas derivados inibidos |
|---|---|
| Postgres down | runtime, scheduler, freshness, replay, queue, research |
| Redis down | runtime, scheduler, queue, freshness, research |
| Data Core API down | scheduler, freshness, queue, dataset, research, runtime, replay |
| Scheduler heartbeat missing/dead | freshness, queue, research, dataset, replay |

## Cooldowns Recomendados

| Classe | Repeat/cooldown |
|---|---:|
| Critical operations | 2h |
| Warning operations | 6h |
| Research edge detected | 12h |
| Research deterioration/divergence/regime change | 6h |
| Dataset quality | 12h |
| Outcome backlog | 4h |
| Statistical significance | 24h |
| Research stalled / growth anomalies | 4h-6h |

## Thresholds Mantidos ou Adicionados

Trading thresholds nao foram alterados.

| Threshold | Valor |
|---|---:|
| Edge minimum sample | 50 |
| Edge profit factor advisory | 1.15 |
| Edge deterioration PF drop | 20% |
| Statistical significance min N | 50 |
| Statistical significance z | 1.645 |
| Research stalled | 8h |
| Research growth window | 24h |
| Acceptance rate low/high | 2% / 80% |
| Acceptance rate min decisions | 50 |
| Regime missing ratio | 20% |
| Regime missing min decisions | 30 |

## Arquivos Alterados

| Area | Arquivo |
|---|---|
| Alertmanager template | `data-core/alertmanager/poupi_telegram.tmpl` |
| Alertmanager routing/dedup | `data-core/alertmanager/alertmanager.telegram-first.yml` |
| Data Core rules | `data-core/prometheus/rules/*.yml` |
| Outcome cleanup | `data-core/prometheus/rules/outcome_pipeline_alerts.yml` |
| Crypto runtime alert | `data-core/prometheus/rules/poupi-crypto-runtime-alerts.yml` |
| Immediate Data Core alerts | `data-core/app/telegram_summary/formatters/alert_formatter.py` |
| Watchdog alerts | `data-core/app/watchdog/heartbeat.py` |
| Logical channel labels | `data-core/app/telegram_summary/channel_resolver.py` |
| Crypto Telegram metrics | `poupi-crypto/app/metrics.py` |
| Crypto Telegram publisher | `poupi-crypto/app/notifications/telegram.py` |
| Research alert engine | `poupi-crypto/app/research/alerts.py` |
| Crypto observability rules | `poupi-crypto/monitoring/alerts/poupi-crypto-observability.yml` |

## Plano de Deploy

1. Validar YAML das rules com `promtool check rules`.
2. Validar Alertmanager com `amtool check-config`.
3. Sincronizar o template versionado para o Alertmanager ativo:
   `data-core/alertmanager/poupi_telegram.tmpl` -> `C:/Users/dev/monitoring/templates/poupi_telegram.tmpl`.
4. Sincronizar ou renderizar `alertmanager.telegram-first.yml` para o Alertmanager ativo sem expor secrets.
5. Reiniciar/recarregar Prometheus e Alertmanager.
6. Enviar um alerta de teste warning e um critical.
7. Confirmar no Telegram que cada mensagem mostra Sistema, Problema, Impacto, Urgencia, Acao, Evidencia e Dashboard.
8. Observar 24h de volume para ajustar somente ruido operacional, sem mexer em estrategia/trading.

## Validacao

Validacao manual realizada nos arquivos de template/rules e formatadores alterados.
A validacao automatica por `py_compile` ficou bloqueada por timeout no sandbox desta sessao.
O acesso de escrita para `C:/Users/dev/monitoring` tambem ficou bloqueado por timeout; por isso a
sincronizacao do Alertmanager ativo permanece como passo explicito de deploy.

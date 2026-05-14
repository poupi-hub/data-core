# Collection Operations

Este projeto usa a RAW Layer como fonte de verdade. Todo target ativo deve fechar o fluxo:

Collector -> RAW -> Normalized -> Analytics -> Coverage/Readiness

## Importar Targets

Use um arquivo JSON ou CSV com `module`, `source_name`, `collector_name`, `target_url`, `active` e `metadata_json`.

Exemplo atual:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\import-targets.ps1 -Path .\examples\poupi-baby-targets.json
```

O import valida:

- URL `http(s)` valida.
- compatibilidade entre `source_name` e dominio da URL.
- duplicatas dentro do arquivo.
- metadata recomendada em targets ativos: `owner`, `category`, `product_seed`.

Linhas invalidas sao retornadas em `errors` e nao sao salvas. Warnings nao bloqueiam.

## Validar Coleta

Validacao completa com coleta real:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\validate-collection.ps1 -Module ecommerce -CollectorName poupi_legacy_raw_collector
```

Validacao sem executar coleta nova:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\validate-collection.ps1 -Module ecommerce -CollectorName poupi_legacy_raw_collector -SkipCollect
```

O comando falha se houver target ativo bloqueado ou readiness falso.

## Rodar Targets Com Limites

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-targets.ps1 -Module ecommerce -CollectorName poupi_legacy_raw_collector -MaxTargets 5 -DelaySeconds 2 -TimeoutSeconds 180
```

Para listar o que seria coletado sem executar scraping:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-targets.ps1 -Module ecommerce -CollectorName poupi_legacy_raw_collector -MaxTargets 5 -ListOnly
```

`MaxTargets`, `DelaySeconds`, `TimeoutSeconds`, `DryRun` e `ListOnly` existem para evitar coleta agressiva e facilitar operacao em producao.

## Smoke Test Poupi Baby

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\smoke-poupi.ps1
```

O smoke test importa `examples/poupi-baby-targets.json`, roda coleta, normalizacao, analytics, readiness, quality por fonte e export de coverage. Para validar apenas o estado atual sem coletar novamente:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\smoke-poupi.ps1 -SkipCollect
```

## Ler Readiness

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\collection-readiness.ps1
```

Readiness responde se os targets ativos estao operacionais agora:

- RAW existe.
- ultimo RAW foi normalizado.
- registro normalizado existe.
- analytics nao esta pendente.
- freshness esta dentro do SLA.
- nao ha erros de collector nao resolvidos.

## Ler Coverage

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\collection-coverage.ps1 -Module ecommerce -CollectorName poupi_legacy_raw_collector
```

Coverage mostra ativos, candidatos, RAW, normalizados, analytics e motivos de bloqueio.

## Ler Qualidade Por Fonte

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\source-quality.ps1 -Module ecommerce -CollectorName poupi_legacy_raw_collector
```

Qualidade por fonte mostra:

- taxa de targets ativos prontos;
- taxa RAW -> Normalized;
- taxa Normalized -> Analytics;
- status `ok`, `attention` ou `standby`.

Targets candidatos/inativos aparecem nos detalhes e nas taxas historicas, mas nao bloqueiam o status `ok` de uma fonte quando todos os targets ativos estao prontos.

## Ler Alertas Operacionais

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\operational-alerts.ps1 -Module ecommerce
```

Alertas cobrem:

- targets ativos sem RAW recente;
- RAW pendente antigo;
- falhas de normalizacao;
- analytics pendente antigo;
- erros de collector nao resolvidos.

## Prometheus e Alertmanager

O compose sobe Prometheus, Grafana e Alertmanager para alertas operacionais. O Alertmanager usa `alertmanager/alertmanager.yml` e envia webhooks para `http://host.docker.internal:9099/alerts` por padrao; ajuste esse destino antes de producao.

Validacao local das regras:

```powershell
docker compose run --rm --no-deps --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
docker compose run --rm --no-deps --entrypoint amtool alertmanager check-config /etc/alertmanager/alertmanager.yml
```

Smoke test de entrega webhook, usando Alertmanager e receiver temporarios na rede Docker:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\smoke-alertmanager.ps1
```

Para ampliar coleta em producao, avance em lotes pequenos: primeiro `MaxTargets 1`, depois `5`, depois `10`, sempre com `DelaySeconds` e conferindo `operational-alerts.ps1` antes de aumentar.

## Avaliar Targets Candidatos

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\candidate-targets.ps1 -Module ecommerce
```

Recomendacoes possiveis:

- `test_candidate`: candidato ainda nao tem RAW.
- `fix_collector`: coleta falhou.
- `fix_parser`: RAW existe, mas nao virou produto normalizado.
- `run_analytics`: produto normalizado existe, mas analytics ainda falta.
- `promote`: candidato tem RAW, produto normalizado e analytics.
- `keep_standby`: manter parado ate investigacao.

## Exportar Relatorios

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\export-collection-coverage.ps1 -Format markdown -Module ecommerce -CollectorName poupi_legacy_raw_collector
powershell -ExecutionPolicy Bypass -File .\scripts\export-collection-coverage.ps1 -Format csv -Module ecommerce -CollectorName poupi_legacy_raw_collector
```

Os arquivos saem em `runtime-data/collection-coverage.md` e `runtime-data/collection-coverage.csv`.

## Promover Candidato Para Ativo

1. Confirme que a URL coleta RAW com `success: true`.
2. Rode o worker para normalizar e calcular analytics.
3. Confirme `collection-coverage` com `normalized_count > 0` e `analytics_count > 0`.
4. Altere `active` para `true` e `metadata_json.kind` para `production_target`.
5. Rode `validate-collection.ps1`.

## Standby

Mercado Livre fica em standby por enquanto. O target permanece como `candidate_target`, porque o scraper legado coleta RAW, mas ainda nao gera produto normalizavel sem estrategia/API propria.

Novos targets de Drogasil, Droga Raia e Pague Menos devem entrar primeiro como `candidate_target` inativos. Promova para `production_target` somente depois de validar coleta, normalizacao e analytics individualmente.

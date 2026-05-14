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

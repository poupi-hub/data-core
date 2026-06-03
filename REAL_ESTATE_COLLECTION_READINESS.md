# Real Estate Collection Readiness

Data: 2026-05-31

## Estado Inicial No Servidor

Consulta em `raw_collections`:

```text
module=real_estate, source_name=apolar, collector_name=ApolarCollector, total=133, latest=2026-05-27
module=real_estate, source_name=generic_real_estate, collector_name=real_estate.generic_listing, total=1, latest=2026-05-15
module=real_estate, source_name=direct_agencies, total=0
```

Registry remoto inicial nao continha `real_estate.direct_agencies`.

## Correcoes Aplicadas

Hot patch nos containers:

```text
collectors/real_estate/*.py
app/real_estate/*.py
collectors/registry.py
collectors/base.py
app/raw/service.py
workers/collector_worker.py
utils/sanitization.py
database/models.py
```

Depois do hot patch, registry reconheceu:

```text
real_estate.direct_agencies
real_estate.zap_imoveis
real_estate.viva_real
real_estate.olx_imoveis
real_estate.imovelweb
```

Config server-side aplicada em `collector_definitions.config`:

```text
server_first_profile = core_sources
agencies = apolar, imobiliariapacheco, imobiliariamaringa, razao
razao.max_pages = 5
```

## Runs

Run 1:

```text
collector_name = real_estate.direct_agencies
started_at = 2026-05-31 17:19:19+00
status = failed
raw_saved_count = 0
error_count = 1
error = idle-in-transaction timeout / SSL connection closed
```

Root cause: `db.refresh(run)` abria transacao antes do scraping longo; a conexao ficava idle-in-transaction ate o Postgres derrubar.

Run 2:

```text
collector_name = real_estate.direct_agencies
started_at = 2026-05-31 17:43:29+00
status = running -> marcado failed manualmente
raw_saved_count = 0
```

Motivo: execucao local de validacao foi interrompida e nao havia processo Python ativo.

Run 3:

```text
execucao com profile core_sources excedeu 15 minutos no cliente local
```

Apos isso, o servidor ficou degradado: TCP/22 respondia, mas SSH nao completava banner em 10s.

## Veredito

**NO-GO para Real Estate server-side**

Criterios minimos nao atingidos:

- run real success: **NAO**
- raw_saved_count > 0: **NAO**
- direct_agencies persistido no banco do servidor: **NAO**
- nenhuma dependencia do notebook: **NAO**

O codigo e o perfil de fontes existem, mas a execucao server-side ainda precisa ser estabilizada com limites agressivos por fonte, timeouts menores e deploy duravel.


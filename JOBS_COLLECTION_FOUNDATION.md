# Jobs Collection Foundation

Data: 2026-05-31

## Estado Inicial

No servidor, antes do hot patch:

```text
registry nao continha jobs.*
raw_collections where module='jobs' = 0
collection_runs para jobs.* = 0
```

## Foundation Local Existente

Coletores presentes no codigo local e copiados para hot patch:

```text
jobs.gupy
jobs.greenhouse
jobs.lever
jobs.smartrecruiters
jobs.ashby
jobs.bamboohr
jobs.recruitee
jobs.workday
jobs.teamtailor
```

Fontes preferenciais e menos agressivas:

```text
Gupy API publica
Greenhouse API publica
Lever API publica
SmartRecruiters API publica
Recruitee API publica
Teamtailor API publica
```

## Banco

Alteracao aplicada:

```text
ALTER TYPE collectordomain ADD VALUE 'jobs'
```

Essa alteracao foi necessaria para permitir `CollectionRun.domain='jobs'`.

## Validacao

Nao foi possivel concluir run real Jobs apos o servidor degradar durante Real Estate.

Evidencia final conhecida:

```text
module=jobs em raw_collections = 0
collection_runs jobs.* = 0
```

## Veredito

**NO-GO para Jobs server-side**

Foundation de codigo existe e o enum foi preparado, mas ainda nao ha evidencia persistida de coleta Jobs no servidor.


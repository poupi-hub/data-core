# Crypto Coin Domain

Backend migrado do projeto `bot-crypto-coin`.

Este namespace preserva a logica de trading, analytics, backtesting, autotune, storage local e conectores de exchange sem misturar esses detalhes com o core do Data Core.

## Mapa

```text
domains/crypto_coin/
  analytics/       # metricas, decisao, relatórios e analise de storage
  autotune/        # otimizacao genetica e scheduler semanal do tuner
  backtesting/     # simulacao e replay a partir do storage
  config/          # Config e load_config do dominio crypto
  core/            # engine, exchange connector, risk, events e schemas
  data/storage/    # storage original do bot, hoje com SQLite
  indicators/      # indicadores tecnicos e multi-timeframe
  infra/           # logger e notifier
  legacy/          # camada src antiga mantida para compatibilidade
  strategies/      # contratos e estrategia trend_following
```

## Integracao com Data Core

- Collector integrado: `crypto.crypto_coin_ohlcv`.
- Worker dedicado: `workers.crypto_coin_worker`.
- API REST geral: use `/api/v1/collectors`, `/api/v1/collectors/crypto.crypto_coin_ohlcv/run`, `/api/v1/runs` e `/api/v1/records`.
- Feed de candles normalizados: `GET /api/v1/crypto/candles-feed`.
- Feed de sinais e indicadores: `GET /api/v1/crypto/signals-feed`.

Fluxo recomendado para consumidores como Poupi Crypto:

```text
crypto.crypto_coin_ohlcv -> raw_collections:marketCandle
  -> normalized_market_candles
  -> trading_analytics
  -> /api/v1/crypto/*-feed
```

Consumidores externos nao devem manter collector ou banco OHLCV proprio enquanto esse fluxo estiver disponivel. Eles devem paginar pelos feeds com `next_cursor` e usar `symbol`, `timeframe` e `source` como filtros operacionais.

## Regras para evoluir

- Codigo novo deve preferir os pacotes modernos (`core`, `analytics`, `backtesting`, `strategies`).
- A pasta `legacy` existe apenas para manter scripts antigos funcionando.
- Nao coloque frontend, dashboard ou HTML neste dominio.
- Nao salve `.env`, banco SQLite ou logs versionados.
- Se uma funcionalidade virar produto SaaS, exponha por API/worker do Data Core, nao por script solto.

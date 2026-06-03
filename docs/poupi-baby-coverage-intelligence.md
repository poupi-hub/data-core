# Poupi Baby Coverage Intelligence Layer

## Inventario

| Metrica | Valor Atual |
|---|---|
| products | `BabyCoverageSnapshot.products` |
| marketplaces | `BabyCoverageSnapshot.marketplaces` |
| raw collections 24h | `BabyCoverageSnapshot.raw_24h` |
| normalized products 24h | `BabyCoverageSnapshot.normalized_24h` |
| price history 24h | `BabyCoverageSnapshot.price_history_24h` |
| catalog coverage rate | `BabyCoverageSnapshot.catalog_coverage_rate` |
| products below target | `BabyCoverageSnapshot.products_below_target` |

Os valores sao calculados pelo job `poupi_baby_coverage_intelligence_job`.

## Metricas Novas

- `baby_coverage_score`
- `baby_coverage_per_product`
- `baby_product_marketplace_active`
- `baby_marketplace_coverage_rate`
- `marketplace_freshness_score`
- `baby_marketplace_last_price_age_seconds`
- `price_history_growth_rate`
- `normalized_success_rate`
- `useful_offer_rate`
- `baby_raw_collections_24h`
- `baby_normalized_products_24h`
- `baby_products_below_coverage_target`

## Alertas Novos

- `BabyCoverageDrop`
- `MarketplaceStale`
- `PriceHistoryNotGrowing`
- `RawOkNormalizedZero`
- `ProductLostMarketplace`

## Deduplicacao

`MarketplaceStale` e tratado como causa raiz `marketplace_down` e inibe:

- `BabyCoverageDrop`
- `ProductLostMarketplace`

## Dashboard

Novo dashboard: `Coverage Overview`.

Widgets:

- cobertura por produto
- cobertura por marketplace
- produtos abaixo da meta
- produtos sem historico recente
- marketplaces stale
- raw vs normalized
- ofertas ativas via useful offer rate
- ofertas desaparecidas via ProductLostMarketplace

## Impacto

Operacional: separa falha tecnica de perda real de valor para o usuario.

Usuario: monitora se continuam existindo comparacoes uteis, preco fresco e historico crescendo.

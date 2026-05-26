-- ============================================================================
-- catalog-cleanup-collection-targets.sql
--
-- data-core: Desativa collection_targets com category = 'baby_food'
-- e qualquer categoria não relacionada a fraldas.
--
-- NÃO remove linhas — apenas seta active = false para interromper
-- a coleta futura sem perder o histórico de configuração.
--
-- Uso:
--   psql $DATA_CORE_DATABASE_URL -f catalog-cleanup-collection-targets.sql
-- ============================================================================

BEGIN;

-- ── 1. DRY-RUN: ver o que será desativado ────────────────────────────────────

\echo '=== [DRY-RUN] TARGETS QUE SERÃO DESATIVADOS ==='
SELECT
  source_name,
  metadata_json->>'category'   AS category,
  COUNT(*)                     AS targets
FROM collection_targets
WHERE active = true
  AND (
    metadata_json->>'category' IS NULL
    OR metadata_json->>'category' NOT IN ('baby', 'fralda', 'fraldas')
  )
GROUP BY source_name, metadata_json->>'category'
ORDER BY targets DESC;

\echo ''
\echo '=== TARGETS ATIVOS QUE SERÃO MANTIDOS (baby/fralda) ==='
SELECT
  source_name,
  metadata_json->>'category'   AS category,
  metadata_json->>'product_seed' AS product_seed
FROM collection_targets
WHERE active = true
  AND metadata_json->>'category' IN ('baby', 'fralda', 'fraldas')
ORDER BY source_name, product_seed;

-- ── 2. Desativar targets não-fralda ──────────────────────────────────────────

UPDATE collection_targets
SET    active = false,
       updated_at = NOW()
WHERE  active = true
  AND  (
    metadata_json->>'category' IS NULL
    OR metadata_json->>'category' NOT IN ('baby', 'fralda', 'fraldas')
  );

DO $$ BEGIN
  RAISE NOTICE 'Targets desativados: %', (
    SELECT COUNT(*) FROM collection_targets
    WHERE active = false
      AND updated_at >= NOW() - INTERVAL '5 seconds'
  );
END $$;

-- ── 3. Relatório final ────────────────────────────────────────────────────────

\echo ''
\echo '=== RELATÓRIO FINAL ==='
SELECT
  active,
  metadata_json->>'category' AS category,
  COUNT(*) AS targets
FROM collection_targets
GROUP BY active, metadata_json->>'category'
ORDER BY active DESC, targets DESC;

COMMIT;

\echo ''
\echo 'Concluído. Targets baby_food e outros não-fralda foram desativados.'
\echo 'Novos targets podem ser adicionados via poupi-baby-targets.json (apenas category=baby).'

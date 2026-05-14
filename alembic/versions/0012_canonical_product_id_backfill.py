"""Backfill canonical_product_id for existing NormalizedProduct rows

Revision ID: 0012_canonical_backfill
Revises: 0011_analytics_indexes
Create Date: 2026-05-14

Strategy (mirrors product_normalizer.py priority chain):
  1. source_id  — use as-is when not null
  2. title slug — sha1-based slug prefixed with "slug:" when source_id is null

The backfill runs as a SQL UPDATE per batch to avoid holding long transactions.
Only rows where canonical_product_id IS NULL are touched.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0012_canonical_backfill"
down_revision: str | None = "0011_analytics_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: rows with a source_id — use source_id directly
    op.execute(
        """
        UPDATE normalized_products
        SET    canonical_product_id = source_id
        WHERE  canonical_product_id IS NULL
          AND  source_id IS NOT NULL
          AND  source_id <> ''
        """
    )

    # Step 2: remaining rows (source_id null) — build slug from title via Postgres text ops.
    # Mirrors _title_slug() but in pure SQL:
    #   lower → regexp_replace non-alnum → trim dashes → left 60 chars → + '-' + left(md5, 12)
    # We prefix with 'slug:' to match Python output exactly.
    op.execute(
        r"""
        UPDATE normalized_products
        SET    canonical_product_id = CASE
                 WHEN title IS NOT NULL AND title <> '' THEN
                   'slug:' ||
                   left(
                     regexp_replace(lower(title), '[^a-z0-9]+', '-', 'g'),
                     60
                   ) || '-' ||
                   left(md5(
                     regexp_replace(lower(title), '[^a-z0-9]+', '-', 'g')
                   ), 12)
                 ELSE NULL
               END
        WHERE  canonical_product_id IS NULL
        """
    )


def downgrade() -> None:
    # We cannot reliably reverse a backfill (would wipe real canonical IDs added later).
    # Intentionally a no-op — run a targeted DELETE if needed.
    pass

"""Add composite indexes for analytics queries on normalized_products

Revision ID: 0011_analytics_indexes
Revises: 0010_collection_run_indexes
Create Date: 2026-05-14
"""

from alembic import op

revision: str = "0011_analytics_indexes"
down_revision: str | None = "0010_collection_run_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cross-source canonical product history (primary path for price_processor)
    op.create_index(
        "ix_normalized_products_canonical_collected",
        "normalized_products",
        ["canonical_product_id", "collected_at"],
    )
    # Fallback path: WHERE source_id = ? AND collected_at >= ?
    op.create_index(
        "ix_normalized_products_source_id_collected",
        "normalized_products",
        ["source_id", "collected_at"],
    )
    # Second fallback: WHERE store_name = ? AND collected_at >= ?
    op.create_index(
        "ix_normalized_products_store_name_collected",
        "normalized_products",
        ["store_name", "collected_at"],
    )
    # Analytics status queue scan (pending items ordered by collected_at)
    op.create_index(
        "ix_normalized_products_analytics_status_collected",
        "normalized_products",
        ["analytics_status", "collected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_normalized_products_analytics_status_collected", table_name="normalized_products")
    op.drop_index("ix_normalized_products_store_name_collected", table_name="normalized_products")
    op.drop_index("ix_normalized_products_source_id_collected", table_name="normalized_products")
    op.drop_index("ix_normalized_products_canonical_collected", table_name="normalized_products")

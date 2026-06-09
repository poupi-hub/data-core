"""Sunset Jobs and Real Estate verticals.

Drop tables that were removed from the codebase on 2026-06-09:
  - real_estate_analytics (app/analytics/models.py)
  - normalized_real_estate_listings (app/normalization/models.py)
  - normalized_job_postings (app/normalization/models.py)
  - real_estate_listings (alembic/versions/0002_real_estate_module.py)
  - real_estate_sources (alembic/versions/0002_real_estate_module.py)
  - real_estate_price_history (alembic/versions/0002_real_estate_module.py)
  - real_estate_raw_pages (alembic/versions/0002_real_estate_module.py)

CollectorDomain enum values 'jobs' and 'real_estate' are intentionally kept
in the Python enum and DB column to preserve historical rows in
collector_definitions, collection_runs, and collected_records.

Revision ID: 0031_sunset_jobs_real_estate
Revises: 74f248e42006
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031_sunset_jobs_real_estate"
down_revision: str | None = "0030_edge_alert_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop analytics table first (FK to normalized_real_estate_listings)
    op.drop_table("real_estate_analytics")

    # Drop normalized table (FK to raw_collections)
    op.drop_table("normalized_real_estate_listings")

    # Drop normalized job postings (FK to raw_collections)
    op.drop_table("normalized_job_postings")

    # Drop legacy real estate tables from 0002_real_estate_module
    # (These tables belonged to app/modules/real_estate/ which is now removed)
    op.execute("DROP TABLE IF EXISTS real_estate_raw_pages CASCADE")
    op.execute("DROP TABLE IF EXISTS real_estate_price_history CASCADE")
    op.execute("DROP TABLE IF EXISTS real_estate_listings CASCADE")
    op.execute("DROP TABLE IF EXISTS real_estate_sources CASCADE")


def downgrade() -> None:
    # Downgrade is intentionally not implemented:
    # restoring these tables would require recreating the entire module.
    raise NotImplementedError(
        "0031_sunset_jobs_real_estate: downgrade not supported — "
        "Jobs and Real Estate modules have been permanently removed."
    )

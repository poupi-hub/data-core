"""
seed_ecommerce_targets.py — Seed (or re-seed) ecommerce collection targets.

Migrates existing poupi_legacy_raw_collector targets to ecommerce.url_scraper
and inserts any missing DEFAULT_COLLECTION_TARGETS from scheduler/jobs.py.

Usage:
    python scripts/seed_ecommerce_targets.py [--dry-run] [--deactivate-legacy]

Options:
    --dry-run           Print what would happen without writing to DB.
    --deactivate-legacy Mark old poupi_legacy_raw_collector targets as inactive.
"""

from __future__ import annotations

import argparse
import sys
import os

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.models import CollectionTarget
from database.session import SessionLocal
from scheduler.jobs import DEFAULT_COLLECTION_TARGETS


def run(dry_run: bool = False, deactivate_legacy: bool = False) -> None:
    db = SessionLocal()
    try:
        inserted = 0
        updated_active = 0
        legacy_deactivated = 0

        for item in DEFAULT_COLLECTION_TARGETS:
            existing = (
                db.query(CollectionTarget)
                .filter(
                    CollectionTarget.module == item["module"],
                    CollectionTarget.source_name == item["source_name"],
                    CollectionTarget.collector_name == item["collector_name"],
                    CollectionTarget.target_url == item["target_url"],
                )
                .one_or_none()
            )
            if existing is None:
                print(f"  INSERT [{item['source_name']}] {item['target_url'][:80]}")
                if not dry_run:
                    db.add(CollectionTarget(**item))
                inserted += 1
            elif not existing.active:
                print(f"  ACTIVATE [{item['source_name']}] {item['target_url'][:80]}")
                if not dry_run:
                    existing.active = True
                updated_active += 1

        if deactivate_legacy:
            legacy = (
                db.query(CollectionTarget)
                .filter(
                    CollectionTarget.collector_name == "poupi_legacy_raw_collector",
                    CollectionTarget.active.is_(True),
                )
                .all()
            )
            for t in legacy:
                print(f"  DEACTIVATE legacy [{t.source_name}] {t.target_url[:80]}")
                if not dry_run:
                    t.active = False
                legacy_deactivated += 1

        if not dry_run:
            db.commit()

        print(
            f"\n{'[DRY RUN] ' if dry_run else ''}Done — "
            f"inserted={inserted}, activated={updated_active}, legacy_deactivated={legacy_deactivated}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed ecommerce collection targets")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--deactivate-legacy", action="store_true", help="Deactivate old poupi_legacy_raw_collector targets")
    args = parser.parse_args()
    run(dry_run=args.dry_run, deactivate_legacy=args.deactivate_legacy)

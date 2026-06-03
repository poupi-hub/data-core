from __future__ import annotations

from datetime import datetime, timezone

from app.normalization.models import NormalizedJobPosting
from app.normalization.services import BaseNormalizer
from app.raw.models import RawCollection


def _parse_remote(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "1", "remote")
    return None


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        from dateutil import parser as dtparser
        return dtparser.parse(str(value))
    except Exception:
        return None


class JobPostingNormalizer(BaseNormalizer):
    module = "jobs"
    normalizer_name = "job_posting_normalizer"
    normalizer_version = "1.0.0"
    normalized_model_classes = (NormalizedJobPosting,)

    def normalize(self, raw: RawCollection) -> dict | None:
        if not raw.raw_content:
            return None
        payload: dict = raw.raw_content if isinstance(raw.raw_content, dict) else {}

        return {
            "external_id": raw.external_id or str(payload.get("id", "")),
            "source": payload.get("source") or raw.source_name or "unknown",
            "company_id": payload.get("company_id"),
            "company_name": payload.get("company_name") or payload.get("company_id"),
            "title": payload.get("title"),
            "department": payload.get("department"),
            "city": payload.get("city"),
            "country": payload.get("country"),
            "remote": _parse_remote(payload.get("remote")),
            "employment_type": payload.get("employment_type"),
            "url": payload.get("url") or raw.source_url,
            "published_at": _parse_dt(payload.get("published_at")),
            "tags": payload.get("tags") or [],
            "collected_at": raw.collected_at,
        }

    def save_normalized(self, raw: RawCollection, normalized: object | list[object] | None) -> int:
        if not isinstance(normalized, dict):
            return 0
        self.db.add(NormalizedJobPosting(raw_collection_id=raw.id, **normalized))
        self.db.flush()
        return 1

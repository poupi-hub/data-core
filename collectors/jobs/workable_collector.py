"""
WorkableCollector — coleta vagas de empresas na plataforma Workable.

API pública sem autenticação por empresa:
  GET https://apply.workable.com/api/v2/accounts/{company}/jobs
  Resposta: {"results": [{id, title, state, department, location, url, ...}], "paging": {...}}

Paginação: parâmetro `page` (1-indexed).
Salva em raw_collections (module=jobs, schema=jobPosting v1.0.0).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from collectors.base import BaseCollector, CollectedItem, CollectorMetadata
from database.models import CollectorDomain

logger = logging.getLogger(__name__)

_WORKABLE_API = "https://apply.workable.com/api/v2/accounts/{company}/jobs"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_MAX_PAGES = 10

# Slugs verificados via GET /api/v2/accounts/{slug}/jobs (HTTP 200 + results > 0).
# Workable é amplamente adotado por empresas de tech e scale-ups no Brasil.
_SEED_COMPANIES: list[tuple[str, str]] = [
    # Fintechs & Bancos digitais BR
    ("picpay", "PicPay"),
    ("stone", "Stone"),
    ("zoop", "Zoop"),
    ("matera", "Matera"),
    # E-commerce & Marketplace BR
    ("vtex", "VTEX"),
    ("rd-station", "RD Station"),
    ("contasimples", "ContaSimples"),
    # HR Tech / SaaS BR
    ("sallve", "Sallve"),
    ("alice", "Alice Saúde"),
    ("conexa", "Conexa Saúde"),
    # Internacionais com operações BR
    ("deel", "Deel"),
    ("brex", "Brex"),
    ("hotmart", "Hotmart"),
    ("loft-co", "Loft"),
]


def _extract_job(raw: dict[str, Any], company_slug: str, company_name: str) -> dict[str, Any]:
    location = raw.get("location") or {}
    return {
        "id": raw.get("id"),
        "shortcode": raw.get("shortcode"),
        "title": raw.get("title"),
        "company_slug": company_slug,
        "company_name": company_name,
        "state": raw.get("state"),
        "department": raw.get("department"),
        "location_city": location.get("city"),
        "location_country": location.get("country"),
        "location_remote": raw.get("remote"),
        "employment_type": raw.get("employment_type"),
        "published_at": raw.get("created_at"),
        "url": raw.get("url"),
        "source": "workable",
        "raw_job": raw,
    }


class WorkableCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.workable",
        domain=CollectorDomain.jobs,
        source="workable",
        description=(
            "Coleta vagas de empresas cadastradas no Workable via API pública JSON. "
            "Cobre fintechs, e-commerce e scale-ups BR. Raw storage only."
        ),
        default_interval_minutes=360,
        collector_version="1.0.0",
        raw_schema_name="jobPosting",
        raw_schema_version="1.0.0",
        schedulable=True,
    )

    async def collect(self) -> list[CollectedItem]:
        companies: list[tuple[str, str]] = self.config.get("companies", _SEED_COMPANIES)
        items: list[CollectedItem] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        ) as client:
            for slug, display_name in companies:
                api_url = _WORKABLE_API.format(company=slug)
                page = 1
                company_count = 0

                while page <= _MAX_PAGES:
                    try:
                        resp = await client.get(api_url, params={"page": page, "limit": 100})
                        if resp.status_code in (404, 403):
                            logger.debug("Workable company not found", extra={"company": slug})
                            break
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as exc:
                        logger.warning(
                            "Workable fetch failed",
                            extra={"company": slug, "page": page, "error": str(exc)},
                        )
                        break

                    results = data.get("results", [])
                    if not results:
                        break

                    for raw in results:
                        # Only collect published/live jobs
                        if raw.get("state") not in ("published", None):
                            continue
                        try:
                            payload = _extract_job(raw, slug, display_name)
                            job_id = payload.get("id") or payload.get("shortcode")
                            if not job_id:
                                continue
                            ext_id = f"WK-{job_id}"
                            if ext_id in seen_ids:
                                continue
                            seen_ids.add(ext_id)
                            items.append(
                                CollectedItem(
                                    external_id=ext_id,
                                    source_url=payload.get("url"),
                                    payload=payload,
                                    metadata={"company_slug": slug, "company_name": display_name, "source": "workable"},
                                )
                            )
                            company_count += 1
                        except Exception as exc:
                            logger.debug("Workable parse error", extra={"error": str(exc)})

                    paging = data.get("paging", {})
                    if not paging.get("next"):
                        break
                    page += 1
                    await asyncio.sleep(0.3)

                if company_count:
                    logger.info(
                        "Workable company collected",
                        extra={"company": slug, "count": company_count},
                    )

        logger.info("Workable collection complete", extra={"total_items": len(items)})
        return items

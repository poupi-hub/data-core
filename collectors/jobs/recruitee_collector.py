"""
RecruiteeCollector — coleta vagas de empresas na plataforma Recruitee.

API pública sem autenticação por empresa:
  GET https://{company}.recruitee.com/api/offers/
  Resposta: {"offers": [{id, title, remote, location, department, tags, url, ...}]}

Paginação: offset/limit na URL.
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

_RECRUITEE_API = "https://{company}.recruitee.com/api/offers/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# Empresas com slugs verificados via API (GET /api/offers/ retorna HTTP 200 + offers > 0).
# Recruitee é popular principalmente na Europa/Nordics.
# Para adicionar novas empresas: verificar em https://recruitee.com/customers
_SEED_COMPANIES = [
    # Verificados como ativos (maio/2026)
    "dock",          # fintech BR — 30+ vagas
    "bunq",          # neobank NL — 30+ vagas
    "channable",     # SaaS NL — 18+ vagas
    "paylogic",      # ticketing NL — 18+ vagas
    "shypple",       # logistics NL — 6+ vagas
    "sendcloud",     # shipping SaaS NL — vagas
    "personio",      # HR SaaS DE — vagas
    # Para expansão futura: buscar clientes Recruitee em https://recruitee.com/customers
    # e testar com script: GET https://{slug}.recruitee.com/api/offers/
]


def _extract_job(raw: dict[str, Any], company: str) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "company_id": company,
        "remote": raw.get("remote"),
        "location": raw.get("location"),
        "city": raw.get("city"),
        "country": raw.get("country"),
        "department": raw.get("department"),
        "tags": raw.get("tags", []),
        "employment_type": raw.get("employment_type_code"),
        "published_at": raw.get("published_at") or raw.get("created_at"),
        "url": raw.get("careers_url") or raw.get("url"),
        "source": "recruitee",
        "raw_job": raw,
    }


class RecruiteeCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.recruitee",
        domain=CollectorDomain.jobs,
        source="recruitee",
        description=(
            "Coleta vagas de empresas cadastradas no Recruitee via API pública JSON. "
            "Cobre empresas de tech e fintech BR. Raw storage only."
        ),
        default_interval_minutes=360,
        collector_version="1.0.0",
        raw_schema_name="jobPosting",
        raw_schema_version="1.0.0",
        schedulable=True,
    )

    async def collect(self) -> list[CollectedItem]:
        companies: list[str] = self.config.get("companies", _SEED_COMPANIES)
        items: list[CollectedItem] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        ) as client:
            for company in companies:
                api_url = _RECRUITEE_API.format(company=company)
                try:
                    resp = await client.get(api_url)
                    if resp.status_code in (404, 403):
                        logger.debug("Recruitee company not found", extra={"company": company})
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning(
                        "Recruitee fetch failed",
                        extra={"company": company, "error": str(exc)},
                    )
                    continue

                offers = data.get("offers", [])
                for raw in offers:
                    try:
                        payload = _extract_job(raw, company)
                        job_id = payload.get("id")
                        if not job_id:
                            continue
                        ext_id = f"RT-{job_id}"
                        if ext_id in seen_ids:
                            continue
                        seen_ids.add(ext_id)
                        items.append(
                            CollectedItem(
                                external_id=ext_id,
                                source_url=payload.get("url"),
                                payload=payload,
                                metadata={"company": company, "source": "recruitee"},
                            )
                        )
                    except Exception as exc:
                        logger.debug("Recruitee parse error", extra={"error": str(exc)})

                logger.info(
                    "Recruitee company collected",
                    extra={"company": company, "count": len(offers)},
                )
                await asyncio.sleep(0.5)

        logger.info(
            "Recruitee collection complete",
            extra={"total_items": len(items), "unique": len(seen_ids)},
        )
        return items

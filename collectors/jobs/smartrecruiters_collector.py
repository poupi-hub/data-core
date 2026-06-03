"""
SmartRecruitersCollector — coleta vagas de empresas na plataforma SmartRecruiters.

API pública sem autenticação:
  GET https://api.smartrecruiters.com/v1/companies/{company}/postings
  Resposta: {"content": [{id, name, department, location, releasedDate, ref, ...}]}

Seed list: empresas brasileiras (e internacionais com vagas BR) conhecidas no SmartRecruiters.
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

_SR_API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# Seeds verificados via probe HTTP 2026-06-01.
# A API SR retorna HTTP 200 para slugs válidos com 0 vagas (empresa sem postings públicos).
# Apenas seeds que retornaram content > 0 permanecem.
# Para expandir: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=5
# Removidos 2026-06-01 (200 + 0 vagas): Adecco, ManpowerGroup, Randstad, Hays, MichaelPage,
#   RobertHalf, Sitel, Teleperformance, Concentrix, Atento, ADP, NGA, Conduent, Stefanini,
#   Diebold, Gartner, Dun-and-Bradstreet, Infor, Pitney-Bowes, Hologic, Crane, nVent,
#   Vertiv, Enovis, Hillenbrand, TriMas, CIRCOR, UFP-Technologies, Watts-Water-Technologies
_SEED_COMPANIES = [
    "smartrecruiters",   # plataforma própria — seed de referência permanente
    "Sodexo",            # 10+ vagas ativas verificadas 2026-06-01
]


def _extract_job(raw: dict[str, Any], company: str) -> dict[str, Any]:
    location = raw.get("location") or {}
    return {
        "id": raw.get("id"),
        "title": raw.get("name"),
        "company_id": company,
        "department": (raw.get("department") or {}).get("label"),
        "city": location.get("city"),
        "country": location.get("country"),
        "region": location.get("region"),
        "remote": location.get("remote"),
        "published_at": raw.get("releasedDate"),
        "url": raw.get("ref"),
        "source": "smartrecruiters",
        "raw_job": raw,
    }


class SmartRecruitersCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.smartrecruiters",
        domain=CollectorDomain.jobs,
        source="smartrecruiters",
        description=(
            "Coleta vagas de empresas cadastradas no SmartRecruiters via API pública. "
            "Cobre empresas brasileiras e multinacionais com vagas BR. Raw storage only."
        ),
        default_interval_minutes=360,  # 6h
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
                url = _SR_API.format(company=company)
                try:
                    resp = await client.get(url, params={"limit": 100, "offset": 0})
                    if resp.status_code == 404:
                        logger.debug("SmartRecruiters company not found", extra={"company": company})
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning(
                        "SmartRecruiters fetch failed",
                        extra={"company": company, "error": str(exc)},
                    )
                    continue

                postings = data.get("content", [])
                for raw in postings:
                    try:
                        payload = _extract_job(raw, company)
                        job_id = payload.get("id")
                        if not job_id:
                            continue
                        ext_id = f"SR-{job_id}"
                        if ext_id in seen_ids:
                            continue
                        seen_ids.add(ext_id)
                        items.append(
                            CollectedItem(
                                external_id=ext_id,
                                source_url=payload.get("url"),
                                payload=payload,
                                metadata={"company": company, "source": "smartrecruiters"},
                            )
                        )
                    except Exception as exc:
                        logger.debug("SmartRecruiters parse error", extra={"error": str(exc)})

                logger.info(
                    "SmartRecruiters company collected",
                    extra={"company": company, "count": len(postings)},
                )
                await asyncio.sleep(0.5)

        logger.info(
            "SmartRecruiters collection complete",
            extra={"total_items": len(items), "unique": len(seen_ids)},
        )
        return items

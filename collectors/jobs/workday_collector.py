"""
WorkdayCollector — coleta vagas de empresas na plataforma Workday.

O Workday usa uma API proprietária via POST JSON específica por empresa.
Cada empresa tem um tenant próprio em:
  https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs

Estratégia:
  1. POST à API de busca de vagas — retorna JSON estruturado
  2. Sem autenticação para postings públicos (jobPostings são públicos por padrão)

Seed list: grandes empresas brasileiras que usam Workday (sabidamente).
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

_WORKDAY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Cada entry: (tenant, workday_number, board_name, display_name)
# tenant = subdomínio da empresa no workday
# wd_num = número do datacenter Workday (1-5)
# board  = nome do board de vagas da empresa
_SEED_COMPANIES: list[dict[str, Any]] = [
    {"tenant": "petrobras", "wd": 1, "board": "Petrobras_External", "name": "Petrobras"},
    {"tenant": "bradescobankbr", "wd": 3, "board": "External", "name": "Bradesco"},
    {"tenant": "itau", "wd": 5, "board": "itau_careers", "name": "Itaú Unibanco"},
    {"tenant": "ambev", "wd": 3, "board": "Ambev_External", "name": "Ambev"},
    {"tenant": "vmware", "wd": 5, "board": "VMware_Lateral", "name": "VMware/Broadcom"},
    {"tenant": "ibm", "wd": 5, "board": "ExternalJobBoard", "name": "IBM Brasil"},
    {"tenant": "accenture", "wd": 1, "board": "AccentureBrasil", "name": "Accenture Brasil"},
    {"tenant": "sap", "wd": 5, "board": "CareersExternal", "name": "SAP Brasil"},
    {"tenant": "amazon", "wd": 1, "board": "External_Career_Site", "name": "Amazon Brasil"},
    {"tenant": "microsoft", "wd": 5, "board": "msexternal", "name": "Microsoft Brasil"},
    {"tenant": "cisco", "wd": 5, "board": "External", "name": "Cisco Brasil"},
    {"tenant": "santander_br", "wd": 3, "board": "External", "name": "Santander Brasil"},
    {"tenant": "brf", "wd": 3, "board": "BRF_External", "name": "BRF"},
    {"tenant": "whirlpool", "wd": 3, "board": "External", "name": "Whirlpool BR"},
    {"tenant": "boticario", "wd": 3, "board": "External", "name": "Boticário"},
]

_JOB_SEARCH_BODY = {
    "appliedFacets": {},
    "limit": 20,
    "offset": 0,
    "searchText": "",
}


def _build_api_url(company: dict[str, Any]) -> str:
    return (
        f"https://{company['tenant']}.wd{company['wd']}.myworkdayjobs.com"
        f"/wday/cxs/{company['tenant']}/{company['board']}/jobs"
    )


def _extract_job(raw: dict[str, Any], company: dict[str, Any]) -> dict[str, Any]:
    locations = raw.get("locationsText", "") or ""
    return {
        "id": raw.get("externalPath", "").split("/")[-1] or raw.get("bulletFields", [""])[0],
        "title": raw.get("title"),
        "company_name": company["name"],
        "company_tenant": company["tenant"],
        "location": locations,
        "published_at": raw.get("postedOn"),
        "url": (
            f"https://{company['tenant']}.wd{company['wd']}.myworkdayjobs.com"
            f"/en-US/{company['board']}/job{raw.get('externalPath', '')}"
        ),
        "source": "workday",
        "raw_job": raw,
    }


class WorkdayCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.workday",
        domain=CollectorDomain.jobs,
        source="workday",
        description=(
            "Coleta vagas de grandes empresas BR que usam Workday via API pública. "
            "Usa seed list de tenants conhecidos. Raw storage only."
        ),
        default_interval_minutes=480,  # 8h
        collector_version="1.0.0",
        raw_schema_name="jobPosting",
        raw_schema_version="1.0.0",
        schedulable=True,
    )

    async def collect(self) -> list[CollectedItem]:
        companies: list[dict] = self.config.get("companies", _SEED_COMPANIES)
        items: list[CollectedItem] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(
            headers=_WORKDAY_HEADERS,
            timeout=httpx.Timeout(25.0),
            follow_redirects=True,
        ) as client:
            for company in companies:
                api_url = _build_api_url(company)
                try:
                    resp = await client.post(api_url, json=_JOB_SEARCH_BODY)
                    if resp.status_code in (404, 403, 400):
                        logger.debug(
                            "Workday company not accessible",
                            extra={"company": company["name"], "status": resp.status_code},
                        )
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning(
                        "Workday fetch failed",
                        extra={"company": company["name"], "error": str(exc)},
                    )
                    continue

                job_postings = data.get("jobPostings", [])
                total = data.get("total", len(job_postings))

                for raw in job_postings:
                    try:
                        payload = _extract_job(raw, company)
                        job_id = payload.get("id") or ""
                        if not job_id:
                            continue
                        ext_id = f"WD-{company['tenant']}-{job_id}"
                        if ext_id in seen_ids:
                            continue
                        seen_ids.add(ext_id)
                        items.append(
                            CollectedItem(
                                external_id=ext_id,
                                source_url=payload.get("url"),
                                payload=payload,
                                metadata={
                                    "company": company["name"],
                                    "tenant": company["tenant"],
                                    "source": "workday",
                                },
                            )
                        )
                    except Exception as exc:
                        logger.debug("Workday parse error", extra={"error": str(exc)})

                logger.info(
                    "Workday company collected",
                    extra={"company": company["name"], "count": len(job_postings), "total": total},
                )
                await asyncio.sleep(1.0)

        logger.info(
            "Workday collection complete",
            extra={"total_items": len(items), "unique": len(seen_ids)},
        )
        return items

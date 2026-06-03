"""
AshbyCollector — coleta vagas de empresas na plataforma Ashby.

Ashby usa um endpoint GraphQL público por organização:
  POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams
  Body: {"operationName":"ApiJobBoardWithTeams",
         "variables":{"organizationHostedJobsPageName":"<slug>"},
         "query":"..."}

Sem autenticação. Disponível para qualquer empresa hospedada no Ashby.
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

_ASHBY_API = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"

_GQL_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    teams {
      name
      parentTeamName
    }
    jobPostings {
      id
      title
      teamName
      locationName
      isRemote
      employmentType
      descriptionPlain
      publishedDate
      applyUrl: applicationLink
    }
  }
}
"""

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://jobs.ashbyhq.com",
    "Referer": "https://jobs.ashbyhq.com/",
}

# Empresas com carreiras no Ashby — startups internacionais com forte presença no Brasil
_SEED_COMPANIES = [
    "remote",
    "deel",
    "rippling",
    "linear",
    "loom",
    "mercury",
    "retool",
    "dbt-labs",
    "runway",
    "brex",
    "ramp",
    "vercel",
    "fly-io",
    "temporal",
    "modal",
    "supabase",
    "planetscale",
    "turso",
    "warp",
    "raycast",
    "hotmart",
    "pipefy",
    "contabilizei",
    "olist",
    "cora",
    "stark-bank",
    "loja-integrada",
    "solfacil",
    "klavi",
    "tractian",
]


def _extract_job(raw: dict[str, Any], company: str) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "company_id": company,
        "department": raw.get("teamName"),
        "location": raw.get("locationName"),
        "remote": raw.get("isRemote"),
        "employment_type": raw.get("employmentType"),
        "published_at": raw.get("publishedDate"),
        "url": raw.get("applyUrl") or f"https://jobs.ashbyhq.com/{company}/{raw.get('id')}",
        "description": (raw.get("descriptionPlain") or "")[:500],
        "source": "ashby",
        "raw_job": raw,
    }


class AshbyCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.ashby",
        domain=CollectorDomain.jobs,
        source="ashby",
        description=(
            "Coleta vagas de empresas cadastradas no Ashby via API GraphQL pública. "
            "Cobre startups globais com vagas remotas e presença no Brasil. Raw storage only."
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
                payload_body = {
                    "operationName": "ApiJobBoardWithTeams",
                    "variables": {"organizationHostedJobsPageName": company},
                    "query": _GQL_QUERY,
                }
                try:
                    resp = await client.post(_ASHBY_API, json=payload_body)
                    if resp.status_code in (404, 400):
                        logger.debug("Ashby company not found", extra={"company": company})
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning(
                        "Ashby fetch failed",
                        extra={"company": company, "error": str(exc)},
                    )
                    continue

                job_board = (data.get("data") or {}).get("jobBoard") or {}
                postings = job_board.get("jobPostings") or []

                for raw in postings:
                    try:
                        extracted = _extract_job(raw, company)
                        job_id = extracted.get("id")
                        if not job_id:
                            continue
                        ext_id = f"ASHBY-{job_id}"
                        if ext_id in seen_ids:
                            continue
                        seen_ids.add(ext_id)
                        items.append(
                            CollectedItem(
                                external_id=ext_id,
                                source_url=extracted.get("url"),
                                payload=extracted,
                                metadata={"company": company, "source": "ashby"},
                            )
                        )
                    except Exception as exc:
                        logger.debug("Ashby parse error", extra={"error": str(exc)})

                logger.info(
                    "Ashby company collected",
                    extra={"company": company, "count": len(postings)},
                )
                await asyncio.sleep(0.5)

        logger.info(
            "Ashby collection complete",
            extra={"total_items": len(items), "unique": len(seen_ids)},
        )
        return items

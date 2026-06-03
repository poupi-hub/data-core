"""
TeamtailorCollector — coleta vagas de empresas na plataforma Teamtailor.

Teamtailor hospeda páginas de carreira em subdomínios customizados.
A API pública por empresa é acessível via:
  GET https://api.teamtailor.com/v1/jobs?filter[status]=published
  Authorization: Token token={api_key}   ← requer chave da empresa

Como não temos chaves, usamos a abordagem pública de scraping:
  Página de empregos: https://careers.{company}.com (varia)
  OU: https://{company}.teamtailor-career-page.com/api/v1/jobs

Estratégia implementada:
  1. Tentar endpoint JSON da careerPage: /{company}?minimal=true
  2. Tentar scraping HTML com extração de dados do script tag
  3. Registrar status BLOCKED se nenhuma estratégia funcionar

Salva em raw_collections (module=jobs, schema=jobPosting v1.0.0).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector, CollectedItem, CollectorMetadata
from database.models import CollectorDomain

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Formato: {"company": slug, "career_url": URL da página de carreiras}
# Algumas empresas têm domínio customizado, outras usam subdomain do teamtailor
_SEED_COMPANIES: list[dict[str, str]] = [
    {"company": "klarna",    "career_url": "https://jobs.klarna.com"},
    {"company": "spotify",   "career_url": "https://www.lifeatspotify.com/jobs"},
    {"company": "king",      "career_url": "https://careers.king.com"},
    {"company": "mojang",    "career_url": "https://www.minecraft.net/en-us/articles/mojang-studios-jobs"},
    {"company": "ubisoft-br","career_url": "https://www.ubisoft.com/en-us/company/careers"},
    {"company": "ingka",     "career_url": "https://jobs.ingka.com"},
    {"company": "electrolux","career_url": "https://jobs.electroluxgroup.com"},
    {"company": "ericsson",  "career_url": "https://jobs.ericsson.com"},
    {"company": "wolt",      "career_url": "https://careers.wolt.com"},
    {"company": "just-eat",  "career_url": "https://careers.justeat.com"},
    {"company": "truecaller","career_url": "https://careers.truecaller.com"},
    {"company": "mojang-studios", "career_url": "https://www.minecraft.net/en-us/jobs"},
    {"company": "appsflyer", "career_url": "https://www.appsflyer.com/jobs"},
    {"company": "gett",      "career_url": "https://gett.com/il/careers"},
    # Brazilian companies on Teamtailor
    {"company": "neon",      "career_url": "https://neon.teamtailor.com/jobs"},
    {"company": "melio",     "career_url": "https://jobs.meliopayments.com"},
    {"company": "caju",      "career_url": "https://caju.teamtailor.com/jobs"},
    {"company": "swile",     "career_url": "https://swile.teamtailor.com/jobs"},
    {"company": "salsify",   "career_url": "https://salsify.teamtailor.com/jobs"},
    {"company": "plaid",     "career_url": "https://plaid.com/careers"},
]


def _extract_from_json_data(data: Any, company: str, base_url: str) -> list[dict[str, Any]]:
    """Extract job postings from various JSON structures returned by Teamtailor."""
    results = []

    # Handle Teamtailor JSON:API format
    if isinstance(data, dict):
        items_raw = (
            data.get("data", [])
            or data.get("jobs", [])
            or data.get("items", [])
            or (data if isinstance(data, list) else [])
        )
        if isinstance(items_raw, dict):
            items_raw = [items_raw]
        for raw in items_raw:
            attrs = raw.get("attributes", raw)
            job_id = raw.get("id") or attrs.get("id")
            title = attrs.get("title") or attrs.get("name")
            if not title:
                continue
            results.append({
                "id": str(job_id) if job_id else None,
                "title": title,
                "company_id": company,
                "location": attrs.get("location") or attrs.get("locations"),
                "remote": attrs.get("remote-status") or attrs.get("remote"),
                "department": attrs.get("department"),
                "employment_type": attrs.get("employment-type"),
                "published_at": attrs.get("created-at") or attrs.get("published_at"),
                "url": attrs.get("career-page-url") or attrs.get("apply-url") or urljoin(base_url, f"/{job_id}"),
                "source": "teamtailor",
                "raw_job": raw,
            })

    return results


class TeamtailorCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.teamtailor",
        domain=CollectorDomain.jobs,
        source="teamtailor",
        description=(
            "Coleta vagas de empresas na plataforma Teamtailor via scraping HTML/JSON. "
            "Usa seed list de empresas com páginas públicas. Raw storage only."
        ),
        default_interval_minutes=480,
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
            headers=_HEADERS,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        ) as client:
            for company_cfg in companies:
                company = company_cfg["company"]
                career_url = company_cfg["career_url"]
                jobs = await self._collect_company(client, company, career_url)

                for job in jobs:
                    job_id = job.get("id") or ""
                    ext_id = f"TT-{company}-{job_id or re.sub(r'[^a-z0-9]', '', (job.get('title') or '').lower())[:20]}"
                    if ext_id in seen_ids:
                        continue
                    seen_ids.add(ext_id)
                    items.append(
                        CollectedItem(
                            external_id=ext_id,
                            source_url=job.get("url"),
                            payload=job,
                            metadata={"company": company, "source": "teamtailor"},
                        )
                    )

                logger.info(
                    "Teamtailor company collected",
                    extra={"company": company, "count": len(jobs)},
                )
                await asyncio.sleep(0.5)

        logger.info(
            "Teamtailor collection complete",
            extra={"total_items": len(items), "unique": len(seen_ids)},
        )
        return items

    async def _collect_company(
        self,
        client: httpx.AsyncClient,
        company: str,
        career_url: str,
    ) -> list[dict[str, Any]]:
        """Try multiple strategies to extract job listings."""
        # Strategy 1: try JSON API endpoint (Teamtailor standard)
        json_url = career_url.rstrip("/") + ".json"
        try:
            resp = await client.get(json_url, headers={**_HEADERS, "Accept": "application/json"})
            if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                data = resp.json()
                results = _extract_from_json_data(data, company, career_url)
                if results:
                    return results
        except Exception:
            pass

        # Strategy 2: HTML page + JSON-LD / embedded script data
        try:
            resp = await client.get(career_url)
            if resp.status_code != 200:
                logger.debug(
                    "Teamtailor career page not accessible",
                    extra={"company": company, "status": resp.status_code},
                )
                return []

            soup = BeautifulSoup(resp.text, "html.parser")

            # Try JSON-LD
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "")
                    if isinstance(data, list):
                        for item in data:
                            if item.get("@type") == "JobPosting":
                                # Return as structured list
                                return [self._ld_to_job(item, company) for item in data
                                        if isinstance(item, dict) and item.get("@type") == "JobPosting"]
                    elif isinstance(data, dict):
                        if data.get("@type") == "JobPosting":
                            return [self._ld_to_job(data, company)]
                        items_list = data.get("itemListElement", [])
                        jobs_from_ld = [
                            self._ld_to_job(el.get("item") or el, company)
                            for el in items_list
                            if isinstance(el, dict)
                        ]
                        if jobs_from_ld:
                            return jobs_from_ld
                except Exception:
                    pass

            # Strategy 3: find job links in HTML
            job_links = soup.find_all("a", href=re.compile(r"job|career|position|vacancy", re.I))
            if job_links:
                jobs = []
                for link in job_links[:30]:
                    href = link.get("href", "")
                    if not href.startswith("http"):
                        href = urljoin(career_url, href)
                    title_text = link.get_text(strip=True)
                    if title_text and len(title_text) > 3:
                        jobs.append({
                            "id": None,
                            "title": title_text,
                            "company_id": company,
                            "url": href,
                            "source": "teamtailor",
                            "raw_job": {"title": title_text, "url": href},
                        })
                if jobs:
                    return jobs

        except Exception as exc:
            logger.warning(
                "Teamtailor HTML scraping failed",
                extra={"company": company, "error": str(exc)},
            )

        return []

    @staticmethod
    def _ld_to_job(ld: dict[str, Any], company: str) -> dict[str, Any]:
        loc = ld.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = loc.get("address") or {}
        return {
            "id": ld.get("identifier", {}).get("value") if isinstance(ld.get("identifier"), dict) else ld.get("identifier"),
            "title": ld.get("title"),
            "company_name": (ld.get("hiringOrganization") or {}).get("name", company),
            "company_id": company,
            "city": addr.get("addressLocality"),
            "country": addr.get("addressCountry"),
            "employment_type": ld.get("employmentType"),
            "published_at": ld.get("datePosted"),
            "url": ld.get("url"),
            "source": "teamtailor",
            "raw_job": ld,
        }

"""
BambooHRCollector — coleta vagas de empresas na plataforma BambooHR.

BambooHR expõe um feed RSS público por empresa:
  GET https://{company}.bamboohr.com/jobs/feed.php
  Resposta: XML RSS com <item> contendo título, link, localização, departamento, data.

Fallback (se RSS falhar): HTML da página de carreiras
  GET https://{company}.bamboohr.com/careers
  Extrai dados do JSON embutido na página.

Salva em raw_collections (module=jobs, schema=jobPosting v1.0.0).
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from collectors.base import BaseCollector, CollectedItem, CollectorMetadata
from database.models import CollectorDomain

logger = logging.getLogger(__name__)

_BHR_RSS = "https://{company}.bamboohr.com/jobs/feed.php"
_BHR_CAREERS = "https://{company}.bamboohr.com/careers"
_BHR_CAREERS_JSON = "https://{company}.bamboohr.com/careers/list"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/xml,text/xml,text/html,application/json,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

_RSS_NS = {"": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}

# Empresas brasileiras e internacionais com forte presença BR no BambooHR
_SEED_COMPANIES = [
    "resultadosdigitais",
    "contaazul",
    "movidesk",
    "sankhya",
    "totvs",
    "runrun",
    "bling",
    "tiny",
    "nuvemshop",
    "vtex",
    "sievert",
    "sonda",
    "padtec",
    "seniorsolucoes",
    "benner",
    "gupy",
    "feedz",
    "kenoby",
    "qulture",
    "tangerino",
    "jira-software",
    "dock",
    "matera",
    "celcoin",
    "magnetis",
    "monkey-exchange",
    "upnid",
    "payly",
    "bankly",
    "cloudwalk",
]


def _parse_rss(xml_text: str, company: str) -> list[dict[str, Any]]:
    """Parse BambooHR RSS feed and return list of job dicts."""
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return results

    # Support both Atom and RSS formats
    items = root.findall(".//{http://www.w3.org/2005/Atom}entry") or root.findall(".//item")

    for item in items:
        def _text(tag: str, ns: str = "") -> str | None:
            el = item.find(f"{{{ns}}}{tag}" if ns else tag) if ns else item.find(tag)
            return el.text.strip() if el is not None and el.text else None

        # Atom format
        title = (
            _text("title", "http://www.w3.org/2005/Atom")
            or _text("title")
        )
        link_el = item.find("{http://www.w3.org/2005/Atom}link")
        link = (
            (link_el.get("href") if link_el is not None else None)
            or _text("link")
        )
        location = (
            _text("location", "http://purl.org/dc/elements/1.1/")
            or _text("location")
        )
        department = (
            _text("department", "http://purl.org/dc/elements/1.1/")
            or _text("department")
        )
        pub_date = _text("updated", "http://www.w3.org/2005/Atom") or _text("pubDate")

        if title:
            # Extract job ID from URL
            job_id = None
            if link:
                m = re.search(r"/(\d+)/?$", link)
                if m:
                    job_id = m.group(1)

            results.append({
                "id": job_id or re.sub(r"\W+", "_", (title or ""))[:40],
                "title": title,
                "company_id": company,
                "location": location,
                "department": department,
                "published_at": pub_date,
                "url": link,
                "source": "bamboohr",
                "raw_job": {
                    "title": title, "link": link, "location": location,
                    "department": department, "pub_date": pub_date,
                },
            })
    return results


class BambooHRCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.bamboohr",
        domain=CollectorDomain.jobs,
        source="bamboohr",
        description=(
            "Coleta vagas de empresas cadastradas no BambooHR via feed RSS público. "
            "Fallback para página HTML de carreiras. Cobre empresas BR. Raw storage only."
        ),
        default_interval_minutes=480,  # 8h — BambooHR muda menos frequentemente
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
                jobs = await self._collect_company(client, company)
                for job in jobs:
                    job_id = job.get("id") or ""
                    ext_id = f"BHR-{company}-{job_id}"
                    if ext_id in seen_ids:
                        continue
                    seen_ids.add(ext_id)
                    items.append(
                        CollectedItem(
                            external_id=ext_id,
                            source_url=job.get("url"),
                            payload=job,
                            metadata={"company": company, "source": "bamboohr"},
                        )
                    )

                logger.info(
                    "BambooHR company collected",
                    extra={"company": company, "count": len(jobs)},
                )
                await asyncio.sleep(0.5)

        logger.info(
            "BambooHR collection complete",
            extra={"total_items": len(items), "unique": len(seen_ids)},
        )
        return items

    async def _collect_company(
        self, client: httpx.AsyncClient, company: str
    ) -> list[dict[str, Any]]:
        """Try RSS feed first, then HTML fallback."""
        # Strategy 1: RSS feed
        rss_url = _BHR_RSS.format(company=company)
        try:
            resp = await client.get(rss_url)
            if resp.status_code == 200 and (
                "xml" in resp.headers.get("content-type", "")
                or resp.text.strip().startswith("<?xml")
                or "<feed" in resp.text[:200]
                or "<rss" in resp.text[:200]
            ):
                jobs = _parse_rss(resp.text, company)
                if jobs:
                    return jobs
        except Exception as exc:
            logger.debug("BambooHR RSS failed", extra={"company": company, "error": str(exc)})

        # Strategy 2: JSON careers list (some instances expose this)
        json_url = _BHR_CAREERS_JSON.format(company=company)
        try:
            resp = await client.get(json_url, headers={**_HEADERS, "Accept": "application/json"})
            if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                data = resp.json()
                jobs_raw = data if isinstance(data, list) else data.get("result", data.get("positions", []))
                results = []
                for raw in (jobs_raw if isinstance(jobs_raw, list) else []):
                    results.append({
                        "id": str(raw.get("id", "")),
                        "title": raw.get("title") or raw.get("jobOpening"),
                        "company_id": company,
                        "location": raw.get("location"),
                        "department": raw.get("department"),
                        "employment_type": raw.get("employmentType"),
                        "url": raw.get("jobUrl") or _BHR_CAREERS.format(company=company),
                        "source": "bamboohr",
                        "raw_job": raw,
                    })
                if results:
                    return results
        except Exception as exc:
            logger.debug("BambooHR JSON fallback failed", extra={"company": company, "error": str(exc)})

        return []

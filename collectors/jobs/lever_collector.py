"""
LeverCollector — coleta vagas de emprego de empresas que usam Lever.

Lever oferece uma API pública por empresa sem autenticação.
Endpoint: GET https://api.lever.co/v0/postings/{company}?mode=json

Usa seed list de empresas brasileiras de tecnologia no Lever.
Para cada empresa, coleta todas as vagas ativas.

Campos coletados: id, título, empresa, localização, departamento, compromisso,
descrição (HTML + plain), data criação, url, atributos, raw_json completo.

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

_LEVER_API = "https://api.lever.co/v0/postings/{company}"
_DEFAULT_TIMEOUT = 20.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Seed list verificada via probe HTTP 2026-06-01.
# Somente slugs que retornaram HTTP 200 + vagas > 0 permanecem.
# Para expandir: GET https://api.lever.co/v0/postings/{slug}?mode=json
_SEED_COMPANIES: list[tuple[str, str]] = [
    # Fintechs BR
    ("cloudwalk", "CloudWalk"),   # 26 vagas
    ("neon", "Neon"),              # 15 vagas
    # AutoTech LATAM
    ("kavak", "Kavak"),            # 11 vagas
    # Internacionais — ativos
    ("mistral", "Mistral AI"),     # 167 vagas
    ("prismic", "Prismic"),        # 6 vagas
    # DEAD 404 removidos 2026-06-01:
    # pagarme, creditas, hash, localiza, roadpass, solugo, olist, linxcommerce,
    # contaazul, rdstation, omie, loft-co, housi, urby, dasa, wellhub,
    # brex, deel, remote, rippling, lattice
]


def _extract_job(raw: dict[str, Any], company_name: str) -> dict[str, Any]:
    """Normalize a Lever posting dict."""
    categories = raw.get("categories", {}) or {}
    lists = raw.get("lists", []) or []  # requirement sections
    created_at_ms = raw.get("createdAt")

    # Convert epoch ms to ISO string
    created_iso: str | None = None
    if created_at_ms:
        try:
            created_iso = str(int(created_at_ms) // 1000)
        except (ValueError, TypeError):
            created_iso = str(created_at_ms)

    # Build requirements list from structured sections
    requirements: list[dict[str, Any]] = [
        {"title": section.get("text"), "content": section.get("content")}
        for section in lists
    ]

    return {
        "id": raw.get("id"),
        "title": raw.get("text"),
        "company_name": company_name,
        "location": categories.get("location"),
        "department": categories.get("department"),
        "team": categories.get("team"),
        "commitment": categories.get("commitment"),  # full-time, part-time, etc.
        "workplace_type": categories.get("workplaceType"),
        "published_at": created_iso,
        "url": raw.get("hostedUrl"),
        "apply_url": raw.get("applyUrl"),
        "description_html": raw.get("description"),
        "description_plain": raw.get("descriptionPlain"),
        "additional_html": raw.get("additional"),
        "additional_plain": raw.get("additionalPlain"),
        "requirements": requirements,
        "tags": raw.get("tags", []),
        "source": "lever",
        "raw_job": {
            k: v for k, v in raw.items()
            if k not in ("description", "descriptionPlain", "additional", "additionalPlain")
        },
    }


class LeverCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="jobs.lever",
        domain=CollectorDomain.jobs,
        source="lever",
        description=(
            "Coleta vagas de emprego via API pública do Lever. "
            "Seed list de empresas brasileiras de tecnologia. "
            "Raw storage only."
        ),
        default_interval_minutes=360,  # 6h
        collector_version="1.0.0",
        raw_schema_name="jobPosting",
        raw_schema_version="1.0.0",
        schedulable=True,
    )

    async def collect(self) -> list[CollectedItem]:
        companies: list[tuple[str, str]] = self.config.get("companies", _SEED_COMPANIES)
        items: list[CollectedItem] = []

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT),
            follow_redirects=True,
        ) as client:
            for slug, display_name in companies:
                url = _LEVER_API.format(company=slug)
                try:
                    resp = await client.get(url, params={"mode": "json"})
                    if resp.status_code == 404:
                        logger.debug("Lever company not found", extra={"company": slug})
                        continue
                    resp.raise_for_status()
                    jobs = resp.json()
                except Exception as exc:
                    logger.warning(
                        "Lever fetch failed",
                        extra={"company": slug, "error": str(exc)},
                    )
                    continue

                if not isinstance(jobs, list):
                    jobs = jobs.get("data", []) if isinstance(jobs, dict) else []

                if not jobs:
                    logger.debug("Lever no jobs", extra={"company": slug})
                    continue

                for raw in jobs:
                    try:
                        payload = _extract_job(raw, display_name)
                        job_id = payload.get("id")
                        if not job_id:
                            continue
                        items.append(
                            CollectedItem(
                                external_id=f"LEVER-{job_id}",
                                source_url=payload.get("url"),
                                payload=payload,
                                metadata={
                                    "company_slug": slug,
                                    "company_name": display_name,
                                    "source": "lever",
                                },
                            )
                        )
                    except Exception as exc:
                        logger.debug(
                            "Lever parse error",
                            extra={"company": slug, "error": str(exc)},
                        )

                logger.info(
                    "Lever company collected",
                    extra={"company": slug, "count": len(jobs)},
                )
                await asyncio.sleep(0.3)

        logger.info("Lever collection complete", extra={"total_items": len(items)})
        return items

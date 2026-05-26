import asyncio
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, outerjoin

from app.analytics.registry import analytics_registry
from app.modules.registry import register_pipeline_modules
from app.normalization.registry import normalizer_registry
from app.raw.models import RawCollection
from core.config import settings
from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus
from database.session import SessionLocal
from workers.collector_worker import run_collector_by_name

logger = logging.getLogger(__name__)


def _log_job_run(
    *,
    job_name: str,
    run_id: str,
    domain: str,
    source: str,
    started_at: float,
    collected_count: int = 0,
    persisted_count: int = 0,
    normalized_count: int = 0,
    failed_count: int = 0,
    retry_count: int = 0,
    error: Exception | None = None,
) -> None:
    """Emit a standardized operational log entry at the end of a scheduled job.

    Fields follow the data-core observability standard (Phase D Fase 5):
      run_id, job, domain, source, status, duration_ms,
      collected_count, persisted_count, normalized_count,
      failed_count, retry_count, last_success_at / last_failure_at.
    """
    duration_ms = int((time.monotonic() - started_at) * 1000)
    now_iso = datetime.now(timezone.utc).isoformat()
    status = "error" if error else ("partial" if failed_count > 0 else "success")

    extra: dict[str, Any] = {
        "run_id": run_id,
        "job": job_name,
        "domain": domain,
        "source": source,
        "status": status,
        "duration_ms": duration_ms,
        "collected_count": collected_count,
        "persisted_count": persisted_count,
        "normalized_count": normalized_count,
        "failed_count": failed_count,
        "retry_count": retry_count,
    }
    if error:
        extra["last_failure_at"] = now_iso
        extra["error"] = str(error)
        logger.error("Job run finished", extra=extra)
    else:
        extra["last_success_at"] = now_iso
        logger.info("Job run finished", extra=extra)


def run_collector_job(collector_name: str) -> None:
    async def _run() -> None:
        db = SessionLocal()
        try:
            await run_collector_by_name(collector_name, db)
        finally:
            db.close()

    logger.info("Starting scheduled collector", extra={"collector": collector_name})
    asyncio.run(_run())


def collect_raw_job(collector_name: str) -> None:
    """Scheduled entry-point for generic collector jobs.

    Emits a standardized operational log entry after execution with fields:
    run_id, domain, source, status, duration_ms, collected_count, persisted_count,
    failed_count — required by Phase D Fase 5 observability standard.
    """
    run_id = str(uuid.uuid4())
    started_at = time.monotonic()
    # Derive domain from collector name prefix (e.g. "crypto.generic_price" → "crypto")
    domain = collector_name.split(".")[0] if "." in collector_name else "unknown"
    logger.info(
        "Job run started",
        extra={"run_id": run_id, "job": "collect_raw_job", "domain": domain, "source": collector_name},
    )
    error: Exception | None = None
    try:
        run_collector_job(collector_name)
    except Exception as exc:
        error = exc
        raise
    finally:
        _log_job_run(
            job_name="collect_raw_job",
            run_id=run_id,
            domain=domain,
            source=collector_name,
            started_at=started_at,
            # collected_count/persisted_count not tracked at this wrapper level;
            # the underlying collector logs them separately via save_raw().
            error=error,
        )


def normalize_job(module: str | None = None, limit: int = 100) -> None:
    from app.pipeline.recorder import PipelineRecorder

    register_pipeline_modules()
    modules = [module] if module else normalizer_registry.modules()
    for module_name in modules:
        for normalizer_type in normalizer_registry.all().get(module_name, []):
            logger.info(
                "Starting normalization job",
                extra={"pipeline_module": module_name, "normalizer": normalizer_type.__name__},
            )
            with PipelineRecorder(
                domain=module_name, stage="normalization",
                source_name=normalizer_type.__name__, trigger="scheduler",
            ) as rec:
                db = SessionLocal()
                try:
                    result = normalizer_type(db).run(limit=limit)
                    if isinstance(result, dict):
                        rec.items_processed = result.get("normalized", 0)
                        rec.items_skipped = result.get("skipped", 0)
                        rec.items_error = result.get("errors", 0)
                        rec.items_input = result.get("loaded_raw", 0)
                    else:
                        rec.items_input = int(getattr(result, "loaded_raw", 0) or 0)
                        rec.items_processed = int(getattr(result, "normalized", 0) or 0)
                        rec.items_error = int(getattr(result, "failed", 0) or 0)
                        rec.items_skipped = max(0, rec.items_input - rec.items_processed - rec.items_error)
                    logger.info("Normalization finished", extra={"pipeline_module": module_name})
                finally:
                    db.close()


MODULE_COLLECTORS = {
    "ecommerce": ["ecommerce.generic_product"],
    "real_estate": ["real_estate.generic_listing"],
    "sports_odds": ["sports_betting.generic_odds"],
    "crypto": ["crypto.generic_price", "crypto.crypto_coin_ohlcv"],
    "trading": [],
}

SOURCE_COLLECTORS = {
    "generic_marketplace": "ecommerce.generic_product",
    "generic_real_estate": "real_estate.generic_listing",
    "generic_bookmaker": "sports_betting.generic_odds",
    "generic_exchange": "crypto.generic_price",
    "crypto_coin_exchange": "crypto.crypto_coin_ohlcv",
}

DEFAULT_COLLECTION_TARGETS = [
    # --- Drogasil (6 targets — BLOCKED 403, active=False para reduzir ruído) ---
    {
        "module": "ecommerce",
        "source_name": "drogasil",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogasil.com.br/fralda-pampers-confort-sec-xxxg-44-unidades-pampers-1351898.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_confort_sec_xxxg_44", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogasil",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogasil.com.br/pampers-fralda-descartavel-confort-sec-pacote-max-xg-com-92-unidades-1250294.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_confort_sec_xg_92", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogasil",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogasil.com.br/fralda-pampers-confort-sec-xg-92-unidades-1474816.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_confort_sec_xg_92_marketplace", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogasil",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogasil.com.br/fralda-pampers-supersec-g-26-unidades-891311.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_supersec_g_26", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogasil",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogasil.com.br/fralda-pampers-premium-care-pants-xg-com-26un-1143334.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_premium_care_pants_xg_26", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogasil",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogasil.com.br/kit-2-fraldas-pampers-supersec-g-26-unidades-666396.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_supersec_g_26_kit_2", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    # --- Drogaraia (6 targets — BLOCKED 403, active=False para reduzir ruído) ---
    {
        "module": "ecommerce",
        "source_name": "drogaraia",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogaraia.com.br/fralda-pampers-confort-sec-xxxg-44-unidades-pampers-1351898.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_confort_sec_xxxg_44", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogaraia",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogaraia.com.br/pampers-premium-care-tamanho-grande-com-30-tiras.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_premium_care_g_30", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogaraia",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogaraia.com.br/fralda-pampers-supersec-g-26-unidades-891311.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_supersec_g_26", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogaraia",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogaraia.com.br/kit-2-fraldas-pampers-supersec-g-26-unidades-666396.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_supersec_g_26_kit_2", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogaraia",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogaraia.com.br/fralda-pampers-premium-care-pants-xg-com-26un-1143334.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_premium_care_pants_xg_26", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    {
        "module": "ecommerce",
        "source_name": "drogaraia",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.drogaraia.com.br/pampers-fralda-descartavel-confort-sec-pacote-max-xg-com-92-unidades-1250294.html",
        "active": False,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "product_seed": "pampers_confort_sec_xg_92", "blocked_reason": "HTTP_403_since_2026-05"},
    },
    # --- Pague Menos — Fraldas Pampers (5 targets existentes + 8 novos) ---
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-descartavel-infantil-pampers-confort-sec-xxxg-mais-de-19kg-pacote-44-unidades-leve-mais-pague-menos/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_confort_sec_xxxg_44"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-pampers-pants-premium-care-m-78-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_premium_care_pants_m_78"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-pampers-confort-sec-p-72-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_confort_sec_p_72"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fraldas-pampers-supersec-p-34-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_supersec_p_34"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fraldas-pampers-premium-care-p-40-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_premium_care_p_40"},
    },
    # Pampers fraldas — tamanhos adicionais (URLs verificadas no catálogo paguemenos 2026-05)
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fraldas-pampers-premium-care-recem-nascido-rnmais-36-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_premium_care_rn_36"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-pampers-confort-sec-g-com-98-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_confort_sec_g_98"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-pampers-pants-premium-care-xg-64-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_premium_care_pants_xg_64"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-infantil-pampers-confort-sec-xg-92-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "pampers", "product_seed": "pampers_confort_sec_xg_92"},
    },
    # --- Pague Menos — Fraldas Huggies ---
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-huggies-natural-care-mega-recem-nascido-com-34-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "huggies", "product_seed": "huggies_natural_care_rn_34"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-roupinha-huggies-supreme-care-m-100-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "huggies", "product_seed": "huggies_supreme_care_m_100"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/fralda-huggies-supreme-care-g-92-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "fraldas", "brand": "huggies", "product_seed": "huggies_supreme_care_g_92"},
    },
    # --- Pague Menos — Lenços Umedecidos ---
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/lencos-umedecidos-pampers-aroma-de-aloe-vera-192-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "lencos_umedecidos", "brand": "pampers", "product_seed": "pampers_lencos_aloe_192"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/lencos-umedecidos-huggies-one-done-leve-mais-por-menos-com-192-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "lencos_umedecidos", "brand": "huggies", "product_seed": "huggies_lencos_192"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/lencos-umedecidos-huggies-pure-care-com-192-unidades/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "lencos_umedecidos", "brand": "huggies", "product_seed": "huggies_pure_care_lencos_192"},
    },
    # --- Pague Menos — Pomadas para Assaduras ---
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/hipoglos-original-pomada-40g/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "pomadas", "brand": "hipoglos", "product_seed": "hipoglos_original_40g"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/bepantol-baby-creme-30g/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "pomadas", "brand": "bepantol", "product_seed": "bepantol_baby_30g"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/desitin-maxima-duracao-57g/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "pomadas", "brand": "desitin", "product_seed": "desitin_max_57g"},
    },
    # --- Pague Menos — Higiene Baby ---
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/sabonete-liquido-de-glicerina-johnsons-baby-da-cabeca-aos-pes-200-ml/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "higiene", "brand": "johnsons", "product_seed": "johnsons_sabonete_glicerina_200ml"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/shampoo-johnsons-baby-cabelos-claros-400-ml/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "higiene", "brand": "johnsons", "product_seed": "johnsons_shampoo_cabelos_claros_400ml"},
    },
    {
        "module": "ecommerce",
        "source_name": "paguemenos",
        "collector_name": "ecommerce.url_scraper",
        "target_url": "https://www.paguemenos.com.br/shampoo-johnsons-baby-regular-400ml/p",
        "active": True,
        "metadata_json": {"kind": "production_target", "owner": "data-platform", "category": "baby", "subcategory": "higiene", "brand": "johnsons", "product_seed": "johnsons_shampoo_regular_400ml"},
    },
]


def run_module_collectors_job(module: str, source: str | None = None) -> None:
    if module == "ecommerce":
        result = run_collection_targets_job(
            module="ecommerce",
            source=source,
            collector_name="ecommerce.url_scraper",
        )
        if result["targets"] > 0:
            logger.info("Ecommerce URL scraper collection finished", extra=result)
            return

    collectors = MODULE_COLLECTORS.get(module, [])
    if source:
        selected = SOURCE_COLLECTORS.get(source)
        collectors = [selected] if selected and selected in collectors else []
    for collector_name in collectors:
        collect_raw_job(collector_name)


def run_ecommerce_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("ecommerce", source=source)


def run_real_estate_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("real_estate", source=source)


def run_sports_odds_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("sports_odds", source=source)


def run_crypto_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("crypto", source=source)


def run_trading_collectors_job(source: str | None = None) -> None:
    run_module_collectors_job("trading", source=source)


def ensure_default_collection_targets() -> int:
    """Insert or activate default collection targets and deactivate stale legacy ones.

    For each entry in DEFAULT_COLLECTION_TARGETS:
      - If the exact record (module+source_name+collector_name+target_url) exists → ensure active.
      - If it does not exist → insert it.
      - Any other record with the same (module, source_name, target_url) but a DIFFERENT
        collector_name (e.g. "poupi_legacy_raw_collector") is deactivated so the scheduler
        no longer picks it up.

    Returns the number of newly created records.
    """
    db = SessionLocal()
    created = 0
    deactivated = 0
    try:
        for item in DEFAULT_COLLECTION_TARGETS:
            # Exact match (correct collector_name)
            exact = (
                db.query(CollectionTarget)
                .filter(
                    CollectionTarget.module == item["module"],
                    CollectionTarget.source_name == item["source_name"],
                    CollectionTarget.collector_name == item["collector_name"],
                    CollectionTarget.target_url == item["target_url"],
                )
                .one_or_none()
            )
            item_active = item.get("active", True)
            if exact is None:
                db.add(CollectionTarget(**item))
                created += 1
            elif not exact.active and item_active:
                # Only re-enable if the seed record itself is active.
                exact.active = True
            elif exact.active and not item_active:
                # Enforce permanent deactivation for blocked providers
                # (active=False in DEFAULT_COLLECTION_TARGETS = HTTP 403 / no bypass).
                exact.active = False

            # Deactivate any other collector pointing at the same URL
            # (e.g. old poupi_legacy_raw_collector targets from before Phase B)
            n = (
                db.query(CollectionTarget)
                .filter(
                    CollectionTarget.module == item["module"],
                    CollectionTarget.source_name == item["source_name"],
                    CollectionTarget.target_url == item["target_url"],
                    CollectionTarget.collector_name != item["collector_name"],
                    CollectionTarget.active.is_(True),
                )
                .update({"active": False}, synchronize_session=False)
            )
            deactivated += n

        db.commit()
        if created or deactivated:
            logger.info(
                "Collection targets ensured",
                extra={"created": created, "legacy_deactivated": deactivated},
            )
        return created
    finally:
        db.close()


def run_collection_targets_job(
    module: str | None = None,
    source: str | None = None,
    collector_name: str | None = None,
    limit: int = 100,
    max_targets: int | None = None,
    delay_seconds: float = 0.0,
    timeout_seconds: int | None = None,
    dry_run: bool = False,
    list_only: bool = False,
) -> dict[str, object]:
    ensure_default_collection_targets()
    db = SessionLocal()
    try:
        # Subquery: last collected_at per (source_name, collector_name, target_url)
        last_col = (
            db.query(
                RawCollection.source_name,
                RawCollection.collector_name,
                RawCollection.target_url,
                func.max(RawCollection.collected_at).label("last_collected_at"),
            )
            .group_by(RawCollection.source_name, RawCollection.collector_name, RawCollection.target_url)
            .subquery()
        )
        query = (
            db.query(CollectionTarget)
            .outerjoin(
                last_col,
                (CollectionTarget.source_name == last_col.c.source_name)
                & (CollectionTarget.collector_name == last_col.c.collector_name)
                & (CollectionTarget.target_url == last_col.c.target_url),
            )
            .filter(CollectionTarget.active.is_(True))
            .order_by(last_col.c.last_collected_at.asc().nullsfirst(), CollectionTarget.created_at)
        )
        if module:
            query = query.filter(CollectionTarget.module == module)
        if source:
            query = query.filter(CollectionTarget.source_name == source)
        if collector_name:
            query = query.filter(CollectionTarget.collector_name == collector_name)
        target_limit = max_targets if max_targets is not None else limit
        targets = query.limit(target_limit).all()
        target_rows = [
            {
                "id": str(target.id),
                "module": target.module,
                "source_name": target.source_name,
                "collector_name": target.collector_name,
                "target_url": target.target_url,
            }
            for target in targets
        ]
        if dry_run or list_only:
            return {
                "targets": len(targets),
                "raw_saved_count": 0,
                "error_count": 0,
                "skipped_locked": 0,
                "dry_run": dry_run,
                "list_only": list_only,
                "target_limit": target_limit,
                "targets_detail": target_rows,
            }
        grouped: dict[str, list[CollectionTarget]] = defaultdict(list)
        for target in targets:
            grouped[target.collector_name].append(target)

        raw_saved = 0
        errors = 0
        skipped_locked = 0
        for selected_collector_name, selected_targets in grouped.items():
            available_targets = []
            for target in selected_targets:
                if _has_running_target_lock(db, target):
                    logger.info(
                        "Collection target skipped because an active run exists",
                        extra={"collector": selected_collector_name, "source_name": target.source_name},
                    )
                    skipped_locked += 1
                else:
                    available_targets.append(target)
            if not available_targets:
                continue
            if selected_collector_name == "ecommerce.url_scraper":
                result = _run_python_url_targets(
                    db,
                    available_targets,
                    delay_seconds=delay_seconds,
                    timeout_seconds=timeout_seconds,
                )
                raw_saved += int(result.get("raw_saved_count", 0))
                errors += int(result.get("error_count", 0))
            elif selected_collector_name == "poupi_legacy_raw_collector":
                result = _run_poupi_legacy_targets(
                    db,
                    available_targets,
                    delay_seconds=delay_seconds,
                    timeout_seconds=timeout_seconds,
                )
                result_counts = _coerce_poupi_target_result(result)
                raw_saved += result_counts["raw_saved_count"]
                errors += result_counts["error_count"]
            else:
                logger.warning(
                    "Collection target collector is not supported by target runner",
                    extra={"collector": selected_collector_name},
                )
                errors += len(available_targets)
        return {
            "targets": len(targets),
            "raw_saved_count": raw_saved,
            "error_count": errors,
            "skipped_locked": skipped_locked,
            "dry_run": False,
            "list_only": False,
            "target_limit": target_limit,
        }
    finally:
        db.close()


def run_collection_target_by_id(
    target_id: str,
    *,
    include_inactive: bool = False,
    delay_seconds: float = 0.0,
    timeout_seconds: int | None = None,
) -> dict[str, object]:
    db = SessionLocal()
    try:
        target = db.get(CollectionTarget, target_id)
        if target is None:
            return {
                "targets": 0,
                "raw_saved_count": 0,
                "error_count": 1,
                "skipped_locked": 0,
                "error_message": "collection target not found",
            }
        if not target.active and not include_inactive:
            return {
                "targets": 1,
                "raw_saved_count": 0,
                "error_count": 1,
                "skipped_locked": 0,
                "error_message": "collection target is inactive",
            }
        if _has_running_target_lock(db, target):
            return {
                "targets": 1,
                "raw_saved_count": 0,
                "error_count": 0,
                "skipped_locked": 1,
            }
        if target.collector_name == "ecommerce.url_scraper":
            result = _run_python_url_targets(
                db,
                [target],
                delay_seconds=delay_seconds,
                timeout_seconds=timeout_seconds,
            )
            return {
                "targets": 1,
                "raw_saved_count": int(result.get("raw_saved_count", 0)),
                "error_count": int(result.get("error_count", 0)),
                "skipped_locked": 0,
            }
        if target.collector_name == "poupi_legacy_raw_collector":
            result = _run_poupi_legacy_targets(
                db,
                [target],
                delay_seconds=delay_seconds,
                timeout_seconds=timeout_seconds,
            )
            result_counts = _coerce_poupi_target_result(result)
            return {
                "targets": 1,
                "raw_saved_count": result_counts["raw_saved_count"],
                "error_count": result_counts["error_count"],
                "skipped_locked": 0,
            }
        return {
            "targets": 1,
            "raw_saved_count": 0,
            "error_count": 1,
            "skipped_locked": 0,
            "error_message": f"collector {target.collector_name} is not supported by target runner",
        }
    finally:
        db.close()


def run_ecommerce_url_targets_job(source: str | None = None, limit: int = 100) -> dict[str, object]:
    """Scheduled job: collect all active ecommerce.url_scraper targets (every 2h).

    Emits standardized operational log on completion (Phase D Fase 5):
    run_id, domain, source, status, duration_ms, collected_count (targets),
    persisted_count (raw_saved_count), failed_count (error_count).
    """
    run_id = str(uuid.uuid4())
    started_at = time.monotonic()
    job_source = source or "ecommerce.url_scraper"
    logger.info(
        "Job run started",
        extra={"run_id": run_id, "job": "run_ecommerce_url_targets_job", "domain": "ecommerce", "source": job_source},
    )
    error: Exception | None = None
    result: dict[str, object] = {}
    try:
        result = run_collection_targets_job(
            module="ecommerce",
            source=source,
            collector_name="ecommerce.url_scraper",
            limit=limit,
        )
        return result
    except Exception as exc:
        error = exc
        raise
    finally:
        _log_job_run(
            job_name="run_ecommerce_url_targets_job",
            run_id=run_id,
            domain="ecommerce",
            source=job_source,
            started_at=started_at,
            collected_count=int(result.get("targets", 0)),
            persisted_count=int(result.get("raw_saved_count", 0)),
            failed_count=int(result.get("error_count", 0)),
            error=error,
        )


def run_poupi_legacy_targets_job(source: str | None = None, limit: int = 100) -> dict[str, object]:
    result = run_collection_targets_job(
        module="ecommerce",
        source=source,
        collector_name="poupi_legacy_raw_collector",
        limit=limit,
    )
    logger.info("Poupi legacy target collection finished", extra=result)
    return result


def _run_python_url_targets(
    db,
    targets: list[CollectionTarget],
    *,
    delay_seconds: float = 0.5,
    timeout_seconds: int | None = None,
) -> dict[str, int]:
    """Run EcommerceURLScraper against a list of collection targets."""
    from collectors.ecommerce.url_scraper import EcommerceURLScraper

    scraper = EcommerceURLScraper(
        db,
        timeout_seconds=timeout_seconds or 30,
        retry_attempts=2,
        retry_backoff_seconds=3.0,
        delay_seconds=delay_seconds,
    )
    result = scraper.collect_targets(targets)
    # EcommerceURLScraper saves via begin_nested() + flush() within the outer
    # transaction. Without an explicit commit the session close rolls back all
    # inserts silently (autocommit=False). Commit here after all targets are done.
    db.commit()
    return result


def _run_poupi_legacy_targets(
    db,
    targets: list[CollectionTarget],
    *,
    delay_seconds: float = 0.0,
    timeout_seconds: int | None = None,
) -> dict[str, int]:
    from app.modules.ecommerce.collectors.poupi_legacy_collector import LegacyPoupiTarget, PoupiLegacyRawCollector

    collector = PoupiLegacyRawCollector(
        db,
        timeout_seconds=timeout_seconds or 180,
        retry_attempts=2,
        retry_backoff_seconds=3,
        delay_seconds=delay_seconds,
    )
    result = collector.collect_targets(
        [
            LegacyPoupiTarget(
                url=target.target_url,
                source_name=target.source_name,
                metadata={
                    "collection_target_id": str(target.id),
                    **(target.metadata_json or {}),
                },
            )
            for target in targets
        ]
    )
    return {
        "raw_saved_count": int(result.get("raw_saved_count", 0)),
        "error_count": int(result.get("error_count", 0)),
    }


def _coerce_poupi_target_result(result: int | dict[str, int]) -> dict[str, int]:
    if isinstance(result, int):
        return {"raw_saved_count": result, "error_count": 0}
    return {
        "raw_saved_count": int(result.get("raw_saved_count", 0)),
        "error_count": int(result.get("error_count", 0)),
    }


def _has_running_target_lock(db, target: CollectionTarget, *, ttl_minutes: int = 30) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    return (
        db.query(CollectionRun)
        .filter(
            CollectionRun.module == target.module,
            CollectionRun.source_name.in_([target.source_name, "poupi_legacy"]),
            CollectionRun.collector_name == target.collector_name,
            CollectionRun.status == RunStatus.running,
            CollectionRun.started_at >= cutoff,
        )
        .first()
        is not None
    )


def cleanup_stale_runs_job(ttl_minutes: int = 30) -> None:
    from scheduler.circuit_breaker import check_source_circuit

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    db = SessionLocal()
    try:
        stale = (
            db.query(CollectionRun)
            .filter(CollectionRun.status == RunStatus.running, CollectionRun.started_at < cutoff)
            .all()
        )
        failed_sources: set[tuple[str, str]] = set()
        for run in stale:
            run.status = RunStatus.failed
            run.error_message = f"Forcibly terminated: stale after {ttl_minutes}min"
            run.finished_at = datetime.now(timezone.utc)
            db.add(
                CollectorError(
                    run_id=run.id,
                    collector_name=run.collector_name,
                    error_type="StaleRunTimeout",
                    message=f"Run exceeded stale TTL of {ttl_minutes} minutes without completing",
                    context={"ttl_minutes": ttl_minutes, "started_at": run.started_at.isoformat() if run.started_at else None},
                )
            )
            if run.module and run.source_name:
                failed_sources.add((run.module, run.source_name))

        if stale:
            db.commit()
            logger.warning("Marked stale runs as failed", extra={"count": len(stale)})
            for module, source_name in failed_sources:
                check_source_circuit(db, module=module, source_name=source_name)
    finally:
        db.close()


def data_retention_job(
    raw_retention_days: int = 90,
    normalized_retention_days: int = 180,
    run_retention_days: int = 60,
    lineage_retention_days: int = 180,
    error_retention_days: int = 90,
    batch_size: int = 1000,
) -> dict[str, int]:
    """Delete old processed records to control DB growth.

    Cleans up in dependency order to avoid FK violations:
      1. ProductPriceAnalytics (depends on NormalizedProduct)
      2. NormalizedProduct processed > normalized_retention_days
      3. DataLineage linked to deleted raw/normalized (> lineage_retention_days)
      4. CollectorError resolved > error_retention_days
      5. CollectionRun finished > run_retention_days
      6. RawCollection processed/ignored > raw_retention_days
    """
    from app.normalization.models import NormalizedProduct
    from app.analytics.models import ProductPriceAnalytics
    from app.documentation.models import DataLineage

    now = datetime.now(timezone.utc)
    cutoff_raw = now - timedelta(days=raw_retention_days)
    cutoff_norm = now - timedelta(days=normalized_retention_days)
    cutoff_run = now - timedelta(days=run_retention_days)
    cutoff_lineage = now - timedelta(days=lineage_retention_days)
    cutoff_error = now - timedelta(days=error_retention_days)

    db = SessionLocal()
    totals: dict[str, int] = {
        "deleted_analytics": 0,
        "deleted_normalized": 0,
        "deleted_lineage": 0,
        "deleted_errors": 0,
        "deleted_runs": 0,
        "deleted_raw": 0,
    }
    try:
        # 1. Analytics + normalized products
        old_product_ids = [
            row[0]
            for row in db.query(NormalizedProduct.id)
            .filter(
                NormalizedProduct.analytics_status == "processed",
                NormalizedProduct.collected_at < cutoff_norm,
            )
            .limit(batch_size)
            .all()
        ]
        if old_product_ids:
            totals["deleted_analytics"] = (
                db.query(ProductPriceAnalytics)
                .filter(ProductPriceAnalytics.product_id.in_(old_product_ids))
                .delete(synchronize_session=False)
            )
            totals["deleted_normalized"] = (
                db.query(NormalizedProduct)
                .filter(NormalizedProduct.id.in_(old_product_ids))
                .delete(synchronize_session=False)
            )

        # 2. DataLineage older than cutoff (created_at on DataLineage)
        totals["deleted_lineage"] = (
            db.query(DataLineage)
            .filter(DataLineage.created_at < cutoff_lineage)
            .limit(batch_size)
            .delete(synchronize_session=False)
        )

        # 3. CollectorError resolved and old
        totals["deleted_errors"] = (
            db.query(CollectorError)
            .filter(
                CollectorError.resolved_at.isnot(None),
                CollectorError.resolved_at < cutoff_error,
            )
            .limit(batch_size)
            .delete(synchronize_session=False)
        )

        # 4. CollectionRun finished and old
        totals["deleted_runs"] = (
            db.query(CollectionRun)
            .filter(
                CollectionRun.finished_at.isnot(None),
                CollectionRun.finished_at < cutoff_run,
                CollectionRun.status.in_(["success", "failed"]),
            )
            .limit(batch_size)
            .delete(synchronize_session=False)
        )

        # 5. RawCollection processed/ignored
        old_raw_ids = [
            row[0]
            for row in db.query(RawCollection.id)
            .filter(
                RawCollection.processing_status.in_(["normalized", "ignored"]),
                RawCollection.collected_at < cutoff_raw,
            )
            .limit(batch_size)
            .all()
        ]
        if old_raw_ids:
            totals["deleted_raw"] = (
                db.query(RawCollection)
                .filter(RawCollection.id.in_(old_raw_ids))
                .delete(synchronize_session=False)
            )

        db.commit()
        logger.info("Data retention cleanup complete", extra=totals)
        return totals
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def alert_webhook_job() -> None:
    from app.pipeline_api import _build_alerts_payload
    from core.config import settings
    from notifications.webhook import send_webhook

    if not settings.alert_webhook_url:
        return

    db = SessionLocal()
    try:
        payload = _build_alerts_payload(
            db=db,
            module=None,
            source_name=None,
            raw_freshness_hours=settings.alert_webhook_raw_freshness_hours,
            raw_pending_minutes=settings.alert_webhook_raw_pending_minutes,
            analytics_pending_minutes=settings.alert_webhook_analytics_pending_minutes,
            limit=100,
        )
    finally:
        db.close()

    if not payload.get("has_alerts"):
        return

    send_webhook(
        {
            "source": "data-core",
            "event": "operational_alert",
            "environment": settings.app_env,
            "summary": payload["summary"],
            "alert_count": sum(v for v in payload["summary"].values() if isinstance(v, int)),
            "details_url": f"{settings.api_host}:{settings.api_port}/api/v1/operations/alerts",
        }
    )
    logger.info("Operational alert webhook sent", extra={"summary": payload["summary"]})


def backfill_canonical_product_id_job(batch_size: int = 500) -> dict[str, int]:
    """One-time backfill: set canonical_product_id on rows that still have NULL.

    Mirrors the priority chain in product_normalizer.py:
      1. source_id (used as-is)
      2. _title_slug(title) — slug:…-sha1_12

    Safe to run multiple times — only touches NULL rows.
    """
    from app.normalization.models import NormalizedProduct
    from app.modules.ecommerce.normalizers.product_normalizer import _title_slug

    db = SessionLocal()
    updated_source = 0
    updated_slug = 0
    try:
        while True:
            batch = (
                db.query(NormalizedProduct)
                .filter(NormalizedProduct.canonical_product_id.is_(None))
                .limit(batch_size)
                .all()
            )
            if not batch:
                break
            for product in batch:
                if product.source_id:
                    product.canonical_product_id = product.source_id
                    updated_source += 1
                else:
                    slug = _title_slug(product.title)
                    if slug:
                        product.canonical_product_id = slug
                        updated_slug += 1
            db.commit()

        totals = {"updated_via_source_id": updated_source, "updated_via_slug": updated_slug}
        logger.info("Backfill canonical_product_id concluído", extra=totals)
        return totals
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def analytics_job(module: str | None = None, limit: int = 100) -> None:
    from app.pipeline.recorder import PipelineRecorder

    register_pipeline_modules()
    modules = [module] if module else analytics_registry.modules()
    for module_name in modules:
        processor_type = analytics_registry.get(module_name)
        if not processor_type:
            continue
        logger.info("Starting analytics job", extra={"pipeline_module": module_name})
        with PipelineRecorder(
            domain=module_name, stage="analytics",
            source_name=processor_type.__name__, trigger="scheduler",
        ) as rec:
            db = SessionLocal()
            try:
                result = processor_type(db).run(limit=limit)
                if isinstance(result, dict):
                    rec.items_processed = result.get("processed", 0)
                    rec.items_skipped = result.get("skipped", 0)
                    rec.items_error = result.get("errors", 0)
                    rec.items_input = result.get("loaded_normalized", 0)
                else:
                    rec.items_input = int(getattr(result, "loaded_normalized", 0) or 0)
                    rec.items_processed = int(getattr(result, "processed", 0) or 0)
                    rec.items_error = int(getattr(result, "failed", 0) or 0)
                    rec.items_skipped = max(0, rec.items_input - rec.items_processed - rec.items_error)
                logger.info("Analytics processing finished", extra={"pipeline_module": module_name})
            finally:
                db.close()


# ──────────────────────────────────────────────────────────────────────────────
# Phase WATCHDOG — Operational watchdog jobs
# ──────────────────────────────────────────────────────────────────────────────


def operational_watchdog_job() -> None:
    """Run all watchdog checks every 30-60 min and send immediate Telegram alerts.

    Checks:
      1. Collection freshness per domain
      2. Normalization backlog and success rate
      3. Scraper quality scores, anti-bot detections, structural drift
      4. Telegram publication age (via poupi-baby callback events)

    Sends Telegram immediately for critical alerts.  Persists WatchdogRun to DB.
    Updates Prometheus metrics for Grafana dashboards.
    """
    if not settings.watchdog_enabled:
        logger.debug("operational_watchdog_job: watchdog disabled via settings")
        return

    from app.watchdog.service import WatchdogService

    run_id = str(uuid.uuid4())
    started_at = time.monotonic()
    logger.info(
        "Job run started",
        extra={"run_id": run_id, "job": "operational_watchdog_job", "domain": "platform", "source": "watchdog"},
    )
    error: Exception | None = None
    try:
        db = SessionLocal()
        try:
            svc = WatchdogService(db)
            run = svc.run()
            logger.info(
                "Job run finished",
                extra={
                    "run_id": run_id,
                    "job": "operational_watchdog_job",
                    "domain": "platform",
                    "source": "watchdog",
                    "status": run.overall_status,
                    "alert_count": len(run.alert_codes or []),
                    "duration_ms": run.duration_ms,
                    "last_success_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        finally:
            db.close()
    except Exception as exc:
        error = exc
        logger.error(
            "Job run finished",
            extra={
                "run_id": run_id,
                "job": "operational_watchdog_job",
                "domain": "platform",
                "source": "watchdog",
                "status": "error",
                "error": str(exc),
                "last_failure_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise


def scheduler_heartbeat_job() -> None:
    """Proof-of-execution heartbeat written every 5 min by the scheduler process.

    Unlike a Docker health-check (process alive), this proves that APScheduler
    is actively dispatching jobs.  The timestamp is read by the API container
    via the shared runtime-data volume and exposed on /system-status.
    """
    from app.runtime.scheduler_heartbeat import record_job_execution

    record_job_execution(
        "scheduler_heartbeat_job",
        status="success",
        duration_seconds=0.0,
    )
    logger.debug("scheduler_heartbeat_job: heartbeat written")


def watchdog_heartbeat_job() -> None:
    """Send periodic Telegram health summary (every WATCHDOG_HEARTBEAT_HOURS hours).

    Runs all checks and sends a formatted summary regardless of status:
      ✅ Poupi saudável / ⚠️ Poupi — ATENÇÃO / 🔴 Poupi — CRÍTICO
    """
    if not settings.watchdog_enabled:
        logger.debug("watchdog_heartbeat_job: watchdog disabled via settings")
        return

    from app.watchdog.service import WatchdogService

    run_id = str(uuid.uuid4())
    logger.info(
        "Job run started",
        extra={"run_id": run_id, "job": "watchdog_heartbeat_job", "domain": "platform", "source": "watchdog"},
    )
    try:
        db = SessionLocal()
        try:
            svc = WatchdogService(db)
            sent = svc.heartbeat()
            logger.info(
                "Job run finished",
                extra={
                    "run_id": run_id,
                    "job": "watchdog_heartbeat_job",
                    "domain": "platform",
                    "source": "watchdog",
                    "status": "success",
                    "telegram_sent": sent,
                    "last_success_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        finally:
            db.close()
    except Exception as exc:
        logger.error(
            "Job run finished",
            extra={
                "run_id": run_id,
                "job": "watchdog_heartbeat_job",
                "domain": "platform",
                "source": "watchdog",
                "status": "error",
                "error": str(exc),
                "last_failure_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise

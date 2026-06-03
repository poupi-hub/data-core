"""
Observability services — FASE 3, 4, 5.

Funções de cálculo de saúde, integridade e snapshots longitudinais.
Todos os cálculos são SQL-first (sem ORM pesado) para minimizar overhead.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.observability.models import DatasetIntegrityScore, DatasetSnapshot, SourceHealth

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Expected freshness windows (hours) per collector source
_FRESHNESS_WINDOWS: dict[str, int] = {
    # Jobs — ATS APIs
    "gupy": 6,
    "greenhouse": 8,
    "lever": 8,
    "smartrecruiters": 8,
    "ashby": 8,
    "bamboohr": 12,
    "recruitee": 8,
    "workday": 12,
    "teamtailor": 12,
    # Real Estate
    "direct_agencies": 10,
    "apolar": 10,
    "zap_imoveis": 8,
    "viva_real": 8,
    "olx_imoveis": 8,
    "imovelweb": 8,
}
_DEFAULT_FRESHNESS_WINDOW = 24  # horas

# Jobs: campos de completude esperados
_JOBS_COMPLETENESS_FIELDS = ["title", "company_name", "city", "url"]
# Real Estate: campos de completude esperados
_RE_COMPLETENESS_FIELDS = ["title", "price_sale", "city", "url"]

# Pesos para o score final de integridade
_INTEGRITY_WEIGHTS = {
    "freshness": 0.30,
    "completeness": 0.25,
    "consistency": 0.15,
    "duplication": 0.20,
    "coverage": 0.10,
}


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3 — Source Health
# ─────────────────────────────────────────────────────────────────────────────

def compute_source_health(db: Session, collector_name: str) -> SourceHealth:
    """Calcula e persiste a saúde de um coletor específico."""
    now = datetime.now(timezone.utc)

    # --- Coletar métricas de execução dos últimos 30 dias ---
    runs_row = db.execute(text("""
        SELECT
            COUNT(*)                                        AS total_runs,
            COUNT(*) FILTER (WHERE status = 'success')     AS successful_runs,
            COUNT(*) FILTER (WHERE status = 'failed')      AS failed_runs,
            MAX(started_at) FILTER (WHERE status = 'success') AS last_success,
            MAX(started_at) FILTER (WHERE status = 'failed')  AS last_failure,
            SUM(items_collected)                            AS total_items_from_runs
        FROM collection_runs
        WHERE collector_name = :name
          AND started_at >= NOW() - INTERVAL '30 days'
    """), {"name": collector_name}).fetchone()

    total_runs = runs_row.total_runs or 0
    successful_runs = runs_row.successful_runs or 0
    failed_runs = runs_row.failed_runs or 0
    last_success = runs_row.last_success
    last_failure = runs_row.last_failure
    success_rate = (successful_runs / total_runs) if total_runs > 0 else None

    # --- Volume total de registros coletados ---
    # source_name canônico: buscar em collection_runs (authoritative).
    # Não derivar por string-split pois o nome do coletor pode divergir do
    # source_name real (ex: "crypto.crypto_coin_ohlcv" salva com source_name
    # "crypto_coin_exchange").
    module = collector_name.split(".")[0] if "." in collector_name else "unknown"
    source_name_row = db.execute(text("""
        SELECT source_name FROM collection_runs
        WHERE collector_name = :name AND source_name IS NOT NULL
        ORDER BY started_at DESC LIMIT 1
    """), {"name": collector_name}).fetchone()
    source_name = (
        source_name_row.source_name
        if source_name_row and source_name_row.source_name
        else (collector_name.split(".")[-1] if "." in collector_name else collector_name)
    )

    volume_row = db.execute(text("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE collected_at >= NOW() - INTERVAL '1 day') AS last_day
        FROM raw_collections
        WHERE source_name = :source
    """), {"source": source_name}).fetchone()

    records_total = volume_row.total or 0
    records_last_day = volume_row.last_day or 0

    # --- Crescimento semanal (registros/dia, média 7d) ---
    growth_row = db.execute(text("""
        SELECT COUNT(*) AS cnt_7d
        FROM raw_collections
        WHERE source_name = :source
          AND collected_at >= NOW() - INTERVAL '7 days'
    """), {"source": source_name}).fetchone()
    growth_rate = round((growth_row.cnt_7d or 0) / 7.0, 2)

    # --- Taxa de duplicatas (últimos 7 dias) ---
    dup_row = db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'success')      AS runs_ok,
            SUM(items_collected)                            AS items_in,
            SUM(raw_saved_count)                            AS items_saved
        FROM collection_runs
        WHERE collector_name = :name
          AND started_at >= NOW() - INTERVAL '7 days'
    """), {"name": collector_name}).fetchone()
    items_in = dup_row.items_in or 0
    items_saved = dup_row.items_saved or 0
    if items_in and items_in > 0:
        duplicate_rate = round(1.0 - (items_saved / items_in), 4)
    else:
        duplicate_rate = None

    # --- Calcular health_score (0–100) ---
    health_score, status = _calc_health_score(
        success_rate=success_rate,
        last_success=last_success,
        total_runs=total_runs,
        records_total=records_total,
        growth_rate=growth_rate,
        duplicate_rate=duplicate_rate,
        source_name=source_name,
    )

    # --- Derivar category ---
    category = (
        "jobs" if module == "jobs"
        else "real_estate" if module == "real_estate"
        else module
    )

    # --- Persistir ---
    health = SourceHealth(
        computed_at=now,
        collector_name=collector_name,
        category=category,
        source=source_name,
        last_success=last_success,
        last_failure=last_failure,
        total_runs=total_runs,
        successful_runs=successful_runs,
        failed_runs=failed_runs,
        success_rate=success_rate,
        records_collected=records_total,
        records_last_run=records_last_day,
        growth_rate=growth_rate,
        duplicate_rate=duplicate_rate,
        health_score=health_score,
        status=status,
        details_json={
            "items_in_last_7d": items_in,
            "items_saved_last_7d": items_saved,
            "records_last_day": records_last_day,
            "module": module,
        },
    )
    db.add(health)
    db.flush()
    return health


def _calc_health_score(
    *,
    success_rate: float | None,
    last_success: datetime | None,
    total_runs: int,
    records_total: int,
    growth_rate: float | None,
    duplicate_rate: float | None,
    source_name: str,
) -> tuple[float, str]:
    """Calcula health_score (0–100) e status (HEALTHY/WARNING/DEGRADED/CRITICAL/BLOCKED)."""
    if total_runs == 0 and records_total == 0:
        return 0.0, "BLOCKED"

    score = 0.0
    max_score = 100.0

    # 1. Success rate (40 pontos)
    if success_rate is not None:
        score += success_rate * 40
    elif total_runs > 0:
        score += 0  # falhou em tudo

    # 2. Freshness (30 pontos) — quanto tempo desde o último sucesso
    if last_success:
        now = datetime.now(timezone.utc)
        hours_since = (now - last_success.replace(tzinfo=timezone.utc) if last_success.tzinfo is None else now - last_success).total_seconds() / 3600
        expected_window = _FRESHNESS_WINDOWS.get(source_name, _DEFAULT_FRESHNESS_WINDOW)
        freshness_ratio = max(0.0, 1.0 - (hours_since / (expected_window * 3)))
        score += freshness_ratio * 30
    # se nunca teve sucesso: 0 pontos nesta dimensão

    # 3. Volume (20 pontos)
    if records_total >= 1000:
        score += 20
    elif records_total >= 100:
        score += 15
    elif records_total >= 10:
        score += 10
    elif records_total >= 1:
        score += 5

    # 4. Duplicate rate (10 pontos — penalidade por duplicatas excessivas)
    if duplicate_rate is not None:
        # <= 30% duplicatas: OK; > 90%: sinal de problema
        dup_penalty = min(duplicate_rate * 10, 10)
        score += 10 - dup_penalty
    else:
        score += 5  # neutro se desconhecido

    final = round(min(max(score, 0.0), max_score), 2)

    # Determinar status
    if final >= 80:
        status = "HEALTHY"
    elif final >= 60:
        status = "WARNING"
    elif final >= 40:
        status = "DEGRADED"
    elif total_runs > 0:
        status = "CRITICAL"
    else:
        status = "BLOCKED"

    return final, status


def compute_all_source_health(db: Session) -> list[SourceHealth]:
    """Calcula e persiste saúde de todos os coletores registrados."""
    from collectors.registry import registry

    results = []
    for collector_cls in registry.all():
        name = collector_cls.metadata.name
        try:
            health = compute_source_health(db, name)
            results.append(health)
            logger.info(
                "Source health computed",
                extra={
                    "collector": name,
                    "score": health.health_score,
                    "status": health.status,
                },
            )
        except Exception as exc:
            logger.warning(
                "Source health computation failed",
                extra={"collector": name, "error": str(exc)},
            )
    db.commit()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# FASE 4 — Dataset Integrity
# ─────────────────────────────────────────────────────────────────────────────

def compute_dataset_integrity(
    db: Session,
    dataset: str,  # 'jobs' | 'real_estate'
    source: str | None = None,  # None = agregado
) -> DatasetIntegrityScore:
    """Calcula e persiste pontuações de integridade para um dataset."""
    now = datetime.now(timezone.utc)

    # --- Total de registros ---
    where_clause = "module = :dataset"
    params: dict[str, Any] = {"dataset": dataset}
    if source:
        where_clause += " AND source_name = :source"
        params["source"] = source

    total_row = db.execute(text(
        f"SELECT COUNT(*) AS cnt FROM raw_collections WHERE {where_clause}"
    ), params).fetchone()
    total = total_row.cnt or 0

    if total == 0:
        score_obj = DatasetIntegrityScore(
            computed_at=now,
            dataset=dataset,
            source=source,
            freshness_score=0.0,
            completeness_score=0.0,
            consistency_score=0.0,
            duplication_score=100.0,
            coverage_score=0.0,
            dataset_health_score=0.0,
            total_records=0,
        )
        db.add(score_obj)
        db.flush()
        return score_obj

    # --- Freshness score (0–100) ---
    freshness = _calc_freshness_score(db, dataset, source, params, where_clause)

    # --- Completeness score ---
    completeness, field_counts = _calc_completeness_score(db, dataset, source, params, where_clause, total)

    # --- Consistency score (schema validation) ---
    consistency = _calc_consistency_score(db, dataset, source, params, where_clause)

    # --- Duplication score (100 = zero duplicatas) ---
    dup_row = db.execute(text(
        f"SELECT COUNT(*) AS total, COUNT(DISTINCT checksum) AS unique_ck "
        f"FROM raw_collections WHERE {where_clause}"
    ), params).fetchone()
    dup_total = dup_row.total or 1
    dup_unique = dup_row.unique_ck or 0
    duplicate_count = dup_total - dup_unique
    if dup_total > 0:
        duplication_score = round(100 * (dup_unique / dup_total), 2)
    else:
        duplication_score = 100.0

    # --- Coverage score (crescimento do dataset) ---
    coverage = _calc_coverage_score(db, dataset, source, params, where_clause, total)

    # --- Dataset health score combinado ---
    dataset_health = round(
        freshness * _INTEGRITY_WEIGHTS["freshness"]
        + completeness * _INTEGRITY_WEIGHTS["completeness"]
        + consistency * _INTEGRITY_WEIGHTS["consistency"]
        + duplication_score * _INTEGRITY_WEIGHTS["duplication"]
        + coverage * _INTEGRITY_WEIGHTS["coverage"],
        2,
    )

    score_obj = DatasetIntegrityScore(
        computed_at=now,
        dataset=dataset,
        source=source,
        freshness_score=freshness,
        completeness_score=completeness,
        consistency_score=consistency,
        duplication_score=duplication_score,
        coverage_score=coverage,
        dataset_health_score=dataset_health,
        total_records=total,
        records_with_title=field_counts.get("title", 0),
        records_with_company=field_counts.get("company", 0),
        records_with_location=field_counts.get("location", 0),
        records_with_url=field_counts.get("url", 0),
        records_with_price=field_counts.get("price", 0),
        duplicate_count=duplicate_count,
        details_json={
            "freshness": freshness,
            "completeness": completeness,
            "consistency": consistency,
            "duplication": duplication_score,
            "coverage": coverage,
            "total": total,
            "duplicates": duplicate_count,
        },
    )
    db.add(score_obj)
    db.flush()
    return score_obj


def _calc_freshness_score(
    db: Session, dataset: str, source: str | None,
    params: dict, where: str,
) -> float:
    """Score de freshness: quão recente é o dado mais novo."""
    fresh_row = db.execute(text(
        f"SELECT MAX(collected_at) AS last_collected FROM raw_collections WHERE {where}"
    ), params).fetchone()
    last = fresh_row.last_collected
    if not last:
        return 0.0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    hours_ago = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    source_key = source or dataset
    window = _FRESHNESS_WINDOWS.get(source_key, _DEFAULT_FRESHNESS_WINDOW)
    # Score: 100 se dentro da janela, decai linearmente até 0 em 3× a janela
    if hours_ago <= window:
        return 100.0
    elif hours_ago <= window * 3:
        return round(100 * (1.0 - (hours_ago - window) / (window * 2)), 2)
    return 0.0


def _calc_completeness_score(
    db: Session, dataset: str, source: str | None,
    params: dict, where: str, total: int,
) -> tuple[float, dict[str, int]]:
    """Score de completude: fração de campos-chave preenchidos."""
    field_counts: dict[str, int] = {}

    if dataset == "jobs":
        # Jobs: verificar title, company_name/company_id, city, url
        row = db.execute(text(f"""
            SELECT
                COUNT(*) FILTER (WHERE raw_json->>'title' IS NOT NULL AND raw_json->>'title' != '') AS has_title,
                COUNT(*) FILTER (WHERE
                    raw_json->>'company_name' IS NOT NULL OR raw_json->>'company_id' IS NOT NULL
                ) AS has_company,
                COUNT(*) FILTER (WHERE
                    raw_json->>'city' IS NOT NULL OR raw_json->>'location' IS NOT NULL
                ) AS has_location,
                COUNT(*) FILTER (WHERE raw_json->>'url' IS NOT NULL AND raw_json->>'url' != '') AS has_url
            FROM raw_collections WHERE {where}
        """), params).fetchone()

        field_counts = {
            "title": row.has_title or 0,
            "company": row.has_company or 0,
            "location": row.has_location or 0,
            "url": row.has_url or 0,
        }
        total_fields = len(field_counts)
        filled = sum(v for v in field_counts.values())
        score = round(100 * filled / (total * total_fields), 2) if total * total_fields > 0 else 0.0

    elif dataset == "real_estate":
        # Real Estate: usa structured_fields (extraídos pelo enrichment job)
        # Campos: listing_url, title, listing_type, property_type, price, city, neighborhood
        row = db.execute(text(f"""
            SELECT
                COUNT(*) FILTER (WHERE
                    raw_json->'structured_fields'->>'listing_url' IS NOT NULL
                    AND raw_json->'structured_fields'->>'listing_url' != ''
                ) AS has_url,
                COUNT(*) FILTER (WHERE
                    raw_json->'structured_fields'->>'title' IS NOT NULL
                    AND raw_json->'structured_fields'->>'title' != ''
                ) AS has_title,
                COUNT(*) FILTER (WHERE
                    raw_json->'structured_fields'->>'listing_type' IS NOT NULL
                    AND raw_json->'structured_fields'->>'listing_type' != ''
                ) AS has_listing_type,
                COUNT(*) FILTER (WHERE
                    raw_json->'structured_fields'->>'property_type' IS NOT NULL
                    AND raw_json->'structured_fields'->>'property_type' != ''
                ) AS has_property_type,
                COUNT(*) FILTER (WHERE
                    raw_json->'structured_fields'->>'price' IS NOT NULL
                    AND raw_json->'structured_fields'->>'price' != 'null'
                ) AS has_price,
                COUNT(*) FILTER (WHERE
                    raw_json->'structured_fields'->>'city' IS NOT NULL
                    AND raw_json->'structured_fields'->>'city' != ''
                ) AS has_city,
                COUNT(*) FILTER (WHERE
                    raw_json->'structured_fields'->>'neighborhood' IS NOT NULL
                    AND raw_json->'structured_fields'->>'neighborhood' != ''
                ) AS has_neighborhood
            FROM raw_collections WHERE {where}
        """), params).fetchone()

        field_counts = {
            "listing_url": row.has_url or 0,
            "title": row.has_title or 0,
            "listing_type": row.has_listing_type or 0,
            "property_type": row.has_property_type or 0,
            "price": row.has_price or 0,
            "city": row.has_city or 0,
            "neighborhood": row.has_neighborhood or 0,
        }
        total_fields = len(field_counts)
        filled = sum(v for v in field_counts.values())
        score = round(100 * filled / (total * total_fields), 2) if total * total_fields > 0 else 0.0

    else:
        score = 50.0  # módulo desconhecido — score neutro

    return score, field_counts


def _calc_consistency_score(
    db: Session, dataset: str, source: str | None,
    params: dict, where: str,
) -> float:
    """Score de consistência: fração de registros com schema esperado."""
    if dataset == "jobs":
        # Verifica se raw_json contém pelo menos source e raw_job/raw_data
        row = db.execute(text(f"""
            SELECT COUNT(*) FILTER (
                WHERE raw_json->>'source' IS NOT NULL
                  AND (raw_json->'raw_job' IS NOT NULL OR raw_json->'raw_data' IS NOT NULL)
            ) AS valid_schema,
            COUNT(*) AS total
            FROM raw_collections WHERE {where}
        """), params).fetchone()
    elif dataset == "real_estate":
        row = db.execute(text(f"""
            SELECT COUNT(*) FILTER (
                WHERE raw_json->>'source' IS NOT NULL
                  AND raw_json->>'url' IS NOT NULL
            ) AS valid_schema,
            COUNT(*) AS total
            FROM raw_collections WHERE {where}
        """), params).fetchone()
    else:
        return 50.0

    total = row.total or 1
    valid = row.valid_schema or 0
    return round(100 * valid / total, 2)


def _calc_coverage_score(
    db: Session, dataset: str, source: str | None,
    params: dict, where: str, total: int,
) -> float:
    """Score de cobertura: crescimento do dataset nos últimos 30 dias."""
    # Conta registros dos últimos 30 dias vs total
    growth_row = db.execute(text(f"""
        SELECT COUNT(*) FILTER (WHERE collected_at >= NOW() - INTERVAL '30 days') AS recent
        FROM raw_collections WHERE {where}
    """), params).fetchone()
    recent = growth_row.recent or 0

    # Score: 100 se houve crescimento (qualquer registro recente), escala com volume
    if total == 0:
        return 0.0
    growth_ratio = recent / total
    if growth_ratio >= 0.5:
        return 100.0
    elif growth_ratio >= 0.2:
        return 70.0
    elif growth_ratio > 0:
        return 40.0
    return 0.0


def compute_all_dataset_integrity(db: Session) -> None:
    """Calcula e persiste integridade para todos os datasets e fontes."""
    datasets = ["jobs", "real_estate"]
    for dataset in datasets:
        # Score agregado
        try:
            score = compute_dataset_integrity(db, dataset, source=None)
            logger.info(
                "Dataset integrity computed (aggregate)",
                extra={"dataset": dataset, "score": score.dataset_health_score},
            )
        except Exception as exc:
            logger.warning(
                "Dataset integrity aggregate failed",
                extra={"dataset": dataset, "error": str(exc)},
            )

        # Scores por fonte
        sources_row = db.execute(text(
            "SELECT DISTINCT source_name FROM raw_collections WHERE module = :ds"
        ), {"ds": dataset}).fetchall()
        for row in sources_row:
            src = row.source_name
            try:
                score = compute_dataset_integrity(db, dataset, source=src)
                logger.info(
                    "Dataset integrity computed",
                    extra={"dataset": dataset, "source": src, "score": score.dataset_health_score},
                )
            except Exception as exc:
                logger.warning(
                    "Dataset integrity source failed",
                    extra={"dataset": dataset, "source": src, "error": str(exc)},
                )

    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# FASE 5 — Longitudinal Snapshots
# ─────────────────────────────────────────────────────────────────────────────

def take_daily_snapshot(db: Session) -> list[DatasetSnapshot]:
    """Gera (ou atualiza) o snapshot do dia para todos os datasets/fontes."""
    today = date.today()
    results = []
    datasets = ["jobs", "real_estate"]

    for dataset in datasets:
        # Snapshot por fonte
        sources_row = db.execute(text(
            "SELECT DISTINCT source_name FROM raw_collections WHERE module = :ds"
        ), {"ds": dataset}).fetchall()

        for row in sources_row:
            source = row.source_name
            snap = _upsert_snapshot(db, today, dataset, source)
            results.append(snap)

        # Snapshot agregado (source=None)
        snap = _upsert_snapshot(db, today, dataset, None)
        results.append(snap)

    db.commit()
    logger.info("Daily snapshot taken", extra={"date": str(today), "snapshots": len(results)})
    return results


def _upsert_snapshot(
    db: Session,
    today: date,
    dataset: str,
    source: str | None,
) -> DatasetSnapshot:
    """Cria ou atualiza o snapshot do dia."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    where_clause = "module = :dataset"
    params: dict[str, Any] = {"dataset": dataset}
    if source:
        where_clause += " AND source_name = :source"
        params["source"] = source

    # Total e novos hoje
    counts_row = db.execute(text(f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE collected_at >= CURRENT_DATE) AS today
        FROM raw_collections WHERE {where_clause}
    """), params).fetchone()
    total = counts_row.total or 0
    new_today = counts_row.today or 0

    # Buscar o score de integridade mais recente
    integrity_row = db.execute(text("""
        SELECT dataset_health_score, freshness_score, completeness_score,
               duplication_score, coverage_score
        FROM dataset_integrity_scores
        WHERE dataset = :dataset
          AND (source = :source OR (:source IS NULL AND source IS NULL))
        ORDER BY computed_at DESC
        LIMIT 1
    """), {"dataset": dataset, "source": source}).fetchone()

    health_score = integrity_row.dataset_health_score if integrity_row else None
    freshness = integrity_row.freshness_score if integrity_row else None
    completeness = integrity_row.completeness_score if integrity_row else None
    duplication = integrity_row.duplication_score if integrity_row else None
    coverage = integrity_row.coverage_score if integrity_row else None

    # Upsert via merge/conflict
    existing = db.execute(text("""
        SELECT id FROM dataset_snapshots
        WHERE snapshot_date = :date AND dataset = :dataset
          AND (source = :source OR (:source IS NULL AND source IS NULL))
    """), {"date": today, "dataset": dataset, "source": source}).fetchone()

    if existing:
        db.execute(text("""
            UPDATE dataset_snapshots
            SET record_count = :total,
                new_records_today = :new_today,
                health_score = :health,
                freshness_score = :freshness,
                completeness_score = :completeness,
                duplication_score = :duplication,
                coverage_score = :coverage
            WHERE id = :id
        """), {
            "total": total, "new_today": new_today,
            "health": health_score, "freshness": freshness,
            "completeness": completeness, "duplication": duplication,
            "coverage": coverage, "id": existing.id,
        })
        # Re-fetch
        snap_row = db.execute(text(
            "SELECT * FROM dataset_snapshots WHERE id = :id"
        ), {"id": existing.id}).fetchone()
    else:
        snap = DatasetSnapshot(
            snapshot_date=today,
            dataset=dataset,
            source=source,
            record_count=total,
            new_records_today=new_today,
            health_score=health_score,
            freshness_score=freshness,
            completeness_score=completeness,
            duplication_score=duplication,
            coverage_score=coverage,
        )
        db.add(snap)
        db.flush()
        return snap

    # Return a lightweight object for logging
    snap = DatasetSnapshot(
        snapshot_date=today,
        dataset=dataset,
        source=source,
        record_count=total,
        new_records_today=new_today,
        health_score=health_score,
    )
    return snap

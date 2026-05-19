"""FastAPI router for scraper reliability endpoints.

Routes
──────
GET  /api/v1/scrapers/health        — per-domain health summary
GET  /api/v1/scrapers/domains       — list of known scraper domains with status
GET  /api/v1/scrapers/quality       — recent quality scores per domain
GET  /api/v1/scrapers/drift         — recent drift events
GET  /api/v1/scrapers/diagnostics   — auto-diagnostics for all domains
POST /api/v1/scrapers/drift/resolve — mark a drift event as resolved
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.scrapers.diagnostics import DiagnosticsEngine
from app.scrapers.models import ScraperDriftEvent
from app.raw.models import RawCollection
from database.session import get_db

router = APIRouter(prefix="/api/v1/scrapers", tags=["scrapers"])

_DIAGNOSTICS_ENGINE = DiagnosticsEngine()


# ── /health ───────────────────────────────────────────────────────────────────

@router.get("/health", summary="Per-domain scraper health summary")
def scraper_health(
    hours: int = Query(24, ge=1, le=168, description="Look-back window in hours"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return health signals for every known source that has raw collections."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

    rows = (
        db.query(
            RawCollection.source_name,
            func.count().label("total"),
        )
        .filter(RawCollection.collected_at >= since)
        .group_by(RawCollection.source_name)
        .all()
    )

    drift_counts = (
        db.query(
            ScraperDriftEvent.source_name,
            func.count().label("drift_count"),
        )
        .filter(ScraperDriftEvent.detected_at >= since)
        .filter(ScraperDriftEvent.resolved_at.is_(None))
        .group_by(ScraperDriftEvent.source_name)
        .all()
    )
    drift_map = {r.source_name: r.drift_count for r in drift_counts}

    domains = []
    for row in rows:
        drift_count = drift_map.get(row.source_name, 0)
        domains.append({
            "source_name": row.source_name,
            "total_collections": row.total,
            "open_drift_events": drift_count,
            "status": "degraded" if drift_count > 0 else "ok",
        })

    domains.sort(key=lambda d: d["source_name"])
    return {"window_hours": hours, "domains": domains, "total_domains": len(domains)}


# ── /domains ──────────────────────────────────────────────────────────────────

@router.get("/domains", summary="List known scraper domains with latest collection time")
def scraper_domains(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return all known source names with their last collection timestamp."""
    rows = (
        db.query(
            RawCollection.source_name,
            func.max(RawCollection.collected_at).label("last_collected_at"),
            func.count().label("total_collections"),
        )
        .group_by(RawCollection.source_name)
        .order_by(RawCollection.source_name)
        .all()
    )

    return {
        "domains": [
            {
                "source_name": r.source_name,
                "last_collected_at": r.last_collected_at.isoformat() if r.last_collected_at else None,
                "total_collections": r.total_collections,
            }
            for r in rows
        ]
    }


# ── /quality ──────────────────────────────────────────────────────────────────

@router.get("/quality", summary="Recent payload quality scores stored in metadata_json")
def scraper_quality(
    hours: int = Query(24, ge=1, le=168),
    source_name: str | None = Query(None, description="Filter by source name"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return quality score summaries from raw_collections.metadata_json['quality'].

    The quality dict is stored by url_scraper.py when PayloadQualityScorer
    is integrated.  Rows without quality data are excluded from averages.
    """
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

    query = (
        db.query(RawCollection)
        .filter(RawCollection.collected_at >= since)
        .filter(RawCollection.metadata_json.isnot(None))
        .filter(
            RawCollection.metadata_json.op("->")("quality").isnot(None)
        )
    )
    if source_name:
        query = query.filter(RawCollection.source_name == source_name)

    rows = query.all()

    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        sname = row.source_name
        quality = (row.metadata_json or {}).get("quality", {})
        score = quality.get("score")
        if score is None:
            continue
        if sname not in stats:
            stats[sname] = {"scores": [], "grades": {}}
        stats[sname]["scores"].append(score)
        grade = quality.get("grade", "unknown")
        stats[sname]["grades"][grade] = stats[sname]["grades"].get(grade, 0) + 1

    result = []
    for sname, data in sorted(stats.items()):
        scores = data["scores"]
        result.append({
            "source_name": sname,
            "sample_count": len(scores),
            "avg_score": round(sum(scores) / len(scores), 1),
            "min_score": min(scores),
            "max_score": max(scores),
            "grade_distribution": data["grades"],
        })

    return {"window_hours": hours, "domains": result}


# ── /drift ────────────────────────────────────────────────────────────────────

@router.get("/drift", summary="Recent scraper drift events")
def scraper_drift(
    hours: int = Query(48, ge=1, le=720),
    source_name: str | None = Query(None),
    risk_level: str | None = Query(None, description="low | medium | high | critical"),
    resolved: bool = Query(False, description="Include resolved events"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return recent ScraperDriftEvent records."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

    query = db.query(ScraperDriftEvent).filter(ScraperDriftEvent.detected_at >= since)

    if source_name:
        query = query.filter(ScraperDriftEvent.source_name == source_name)
    if risk_level:
        query = query.filter(ScraperDriftEvent.risk_level == risk_level)
    if not resolved:
        query = query.filter(ScraperDriftEvent.resolved_at.is_(None))

    events = query.order_by(ScraperDriftEvent.detected_at.desc()).limit(limit).all()

    return {
        "window_hours": hours,
        "total": len(events),
        "events": [
            {
                "id": e.id,
                "source_name": e.source_name,
                "collector_name": e.collector_name,
                "module": e.module,
                "drift_type": e.drift_type,
                "risk_level": e.risk_level,
                "description": e.description,
                "field_name": e.field_name,
                "detected_at": e.detected_at.isoformat() if e.detected_at else None,
                "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
            }
            for e in events
        ],
    }


# ── /drift/resolve ────────────────────────────────────────────────────────────

@router.post("/drift/resolve/{event_id}", summary="Mark a drift event as resolved")
def resolve_drift_event(
    event_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Set resolved_at = NOW() for a drift event."""
    event = db.query(ScraperDriftEvent).filter(ScraperDriftEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail=f"Drift event {event_id} not found")
    if event.resolved_at:
        return {"status": "already_resolved", "event_id": event_id, "resolved_at": event.resolved_at.isoformat()}

    event.resolved_at = datetime.now(tz=timezone.utc)
    db.commit()
    return {"status": "resolved", "event_id": event_id, "resolved_at": event.resolved_at.isoformat()}


# ── /diagnostics ──────────────────────────────────────────────────────────────

@router.get("/diagnostics", summary="Auto-diagnostics for all scraper domains")
def scraper_diagnostics(
    hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run rule-based diagnostics for every known domain and return findings."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

    # Get unique source names
    source_names_q = (
        db.query(RawCollection.source_name)
        .filter(RawCollection.collected_at >= since)
        .distinct()
        .all()
    )
    source_names = [r.source_name for r in source_names_q]

    all_diagnostics: list[dict[str, Any]] = []
    for sname in sorted(source_names):
        # Drift events
        drift_events = (
            db.query(ScraperDriftEvent)
            .filter(ScraperDriftEvent.source_name == sname)
            .filter(ScraperDriftEvent.detected_at >= since)
            .filter(ScraperDriftEvent.resolved_at.is_(None))
            .all()
        )
        drift_dicts = [
            {"drift_type": e.drift_type, "risk_level": e.risk_level}
            for e in drift_events
        ]

        # Fallback rate from metadata_json
        raw_rows = (
            db.query(RawCollection)
            .filter(RawCollection.source_name == sname)
            .filter(RawCollection.collected_at >= since)
            .all()
        )
        total = len(raw_rows)
        fallback_strategies = {"meta_css", "og_meta", "unknown"}
        fallback_count = sum(
            1
            for r in raw_rows
            if (r.metadata_json or {}).get("strategy") in fallback_strategies
        )

        # Anti-bot count from metadata_json
        anti_bot_count = sum(
            1
            for r in raw_rows
            if (r.metadata_json or {}).get("anti_bot_detected") is True
        )

        # Avg quality score
        scores = [
            (r.metadata_json or {}).get("quality", {}).get("score")
            for r in raw_rows
            if (r.metadata_json or {}).get("quality", {}).get("score") is not None
        ]
        avg_quality = sum(scores) / len(scores) if scores else None

        findings = _DIAGNOSTICS_ENGINE.evaluate(
            source_name=sname,
            drift_events=drift_dicts,
            fallback_count=fallback_count,
            total_count=total,
            anti_bot_count=anti_bot_count,
            window_hours=hours,
            avg_quality_score=avg_quality,
            scraper_enabled=True,  # TODO: wire to ScraperHealthService when available
        )

        if findings:
            all_diagnostics.append({
                "source_name": sname,
                "findings": [f.to_dict() for f in findings],
            })

    return {
        "window_hours": hours,
        "domains_evaluated": len(source_names),
        "domains_with_issues": len(all_diagnostics),
        "diagnostics": all_diagnostics,
    }

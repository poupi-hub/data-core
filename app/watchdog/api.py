"""FastAPI router for operational watchdog endpoints.

Routes
──────
POST /api/v1/watchdog/report/telegram-published  ← poupi-baby callback
POST /api/v1/watchdog/telegram-alert             ← Alertmanager webhook → Telegram
GET  /api/v1/watchdog/status                     ← current status (last run)
GET  /api/v1/watchdog/runs                       ← run history
GET  /api/v1/watchdog/alerts                     ← aggregated alerts from last run
POST /api/v1/watchdog/heartbeat/send             ← manual trigger heartbeat

Phase 3 — Analytics
GET  /api/v1/watchdog/stats                      ← global + per-service counters
GET  /api/v1/watchdog/incidents                  ← incident list with MTTR
GET  /api/v1/watchdog/healers                    ← per-healer attempt/success breakdown

Phase 4 — Reliability & Forecasting
GET  /api/v1/watchdog/reliability                ← 0-100 score per service
GET  /api/v1/watchdog/forecast                   ← disk/queue/memory ETA forecasts
GET  /api/v1/watchdog/anomalies                  ← detected metric spikes
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.watchdog.models import TelegramPublicationEvent, WatchdogRun
from database.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/watchdog", tags=["watchdog"])


# ── Alertmanager webhook → Telegram ──────────────────────────────────────────

class _AlertmanagerAlert(BaseModel):
    """Single alert within an Alertmanager webhook payload."""
    status: str = "unknown"  # "firing" | "resolved"
    labels: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    startsAt: str = ""
    endsAt: str = ""
    generatorURL: str = ""


class AlertmanagerWebhookPayload(BaseModel):
    """Alertmanager POST /webhook body (v4 format)."""
    version: str = "4"
    groupKey: str = ""
    status: str = "firing"  # "firing" | "resolved"
    receiver: str = ""
    groupLabels: dict[str, Any] = {}
    commonLabels: dict[str, Any] = {}
    commonAnnotations: dict[str, Any] = {}
    externalURL: str = ""
    alerts: list[_AlertmanagerAlert] = []


def _format_alertmanager_telegram(payload: AlertmanagerWebhookPayload) -> str:
    """Format an Alertmanager webhook payload as a Telegram message."""
    emoji = "🔴" if payload.status == "firing" else "✅"
    lines: list[str] = [f"{emoji} *[ALERTMANAGER] {payload.status.upper()}*"]

    if payload.commonLabels:
        severity = payload.commonLabels.get("severity", "unknown")
        lines.append(f"Severity: `{severity}`")

    for alert in payload.alerts:
        alert_name = alert.labels.get("alertname", "?")
        summary = alert.annotations.get("summary", alert_name)
        description = alert.annotations.get("description", "")
        service = alert.labels.get("service", alert.labels.get("job", ""))
        status_icon = "🔴" if alert.status == "firing" else "✅"
        lines.append(f"\n{status_icon} *{alert_name}*")
        if service:
            lines.append(f"  Service: `{service}`")
        lines.append(f"  {summary}")
        if description and description != summary:
            lines.append(f"  _{description}_")

    lines.append(f"\n⏱ {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


def _send_telegram_message(text: str) -> bool:
    """Send a Telegram message using the configured bot token and chat."""
    import json as json_lib
    import urllib.request

    from core.config import settings

    if not settings.telegram_enabled or not settings.telegram_bot_token:
        logger.warning("telegram-alert: Telegram not configured — skipping send")
        return False

    chat_id = settings.telegram_system_chat_id or settings.telegram_chat_id
    if not chat_id:
        logger.warning("telegram-alert: no chat_id configured")
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body = json_lib.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.getcode() == 200
            if not ok:
                logger.warning("telegram-alert: bot API returned %d", resp.getcode())
            return ok
    except Exception as exc:
        logger.error("telegram-alert: failed to send message: %s", exc)
        return False


@router.post(
    "/telegram-alert",
    status_code=200,
    summary="Alertmanager webhook → Telegram (critical alerts only)",
)
def alertmanager_telegram_alert(payload: AlertmanagerWebhookPayload) -> dict[str, Any]:
    """Receive an Alertmanager webhook payload and forward it to Telegram.

    Called by Alertmanager's `telegram-critical` receiver when a rule with
    `channel: telegram` label fires or resolves. Always returns 200 so that
    Alertmanager does not retry on transient send failures.
    """
    logger.info(
        "telegram-alert: received groupKey=%s status=%s alerts=%d",
        payload.groupKey, payload.status, len(payload.alerts),
    )

    if not payload.alerts:
        return {"sent": False, "reason": "no alerts in payload"}

    message = _format_alertmanager_telegram(payload)
    sent = _send_telegram_message(message)

    logger.info("telegram-alert: sent=%s", sent)
    return {
        "sent": sent,
        "status": payload.status,
        "alerts": len(payload.alerts),
        "received_at": datetime.now(tz=timezone.utc).isoformat(),
    }


# ── Request / Response schemas ────────────────────────────────────────────────

class TelegramPublishReport(BaseModel):
    """Payload sent by poupi-baby after each Telegram publish attempt."""

    group_id: str | None = None
    product_id: str | None = None
    offer_id: str | None = None
    marketplace: str | None = None
    price: float | None = None
    deal_score: float | None = None
    status: str                     # "sent" | "failed" | "rate_limited" | "skipped"
    fail_reason: str | None = None
    reported_by: str | None = "poupi-baby"


# ── POST /report/telegram-published ──────────────────────────────────────────

@router.post(
    "/report/telegram-published",
    status_code=201,
    summary="Report a Telegram publication event (poupi-baby callback)",
)
def report_telegram_published(
    payload: TelegramPublishReport,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Called by poupi-baby after each TelegramGroupProcessor execution.

    Stores the event in telegram_publication_events so the watchdog can
    determine last successful publication time without direct DB access.
    """
    from decimal import Decimal

    event = TelegramPublicationEvent(
        group_id=payload.group_id,
        product_id=payload.product_id,
        offer_id=payload.offer_id,
        marketplace=payload.marketplace,
        price=Decimal(str(payload.price)) if payload.price is not None else None,
        deal_score=payload.deal_score,
        status=payload.status,
        fail_reason=payload.fail_reason,
        published_at=datetime.now(tz=timezone.utc),
        reported_by=payload.reported_by,
    )
    db.add(event)
    db.commit()
    return {"status": "recorded", "event_id": event.id}


# ── GET /status ───────────────────────────────────────────────────────────────

@router.get("/status", summary="Current watchdog status (last run)")
def watchdog_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the most recent WatchdogRun with check results."""
    run = (
        db.query(WatchdogRun)
        .order_by(WatchdogRun.run_at.desc())
        .first()
    )
    if not run:
        return {"status": "no_runs", "message": "No watchdog runs recorded yet."}

    return {
        "last_run_at": run.run_at.isoformat(),
        "overall_status": run.overall_status,
        "duration_ms": run.duration_ms,
        "alert_codes": run.alert_codes or [],
        "telegram_sent": run.telegram_sent,
        "check_results": run.check_results or {},
        "error_message": run.error_message,
    }


# ── GET /runs ─────────────────────────────────────────────────────────────────

@router.get("/runs", summary="Watchdog run history")
def watchdog_runs(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return recent WatchdogRun records."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    runs = (
        db.query(WatchdogRun)
        .filter(WatchdogRun.run_at >= since)
        .order_by(WatchdogRun.run_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "window_hours": hours,
        "total": len(runs),
        "runs": [
            {
                "id": r.id,
                "run_at": r.run_at.isoformat(),
                "overall_status": r.overall_status,
                "duration_ms": r.duration_ms,
                "alert_codes": r.alert_codes or [],
                "telegram_sent": r.telegram_sent,
            }
            for r in runs
        ],
    }


# ── GET /alerts ───────────────────────────────────────────────────────────────

@router.get("/alerts", summary="Aggregated alerts from the last watchdog run")
def watchdog_alerts(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the alerts fired in the most recent watchdog run."""
    run = (
        db.query(WatchdogRun)
        .order_by(WatchdogRun.run_at.desc())
        .first()
    )
    if not run:
        return {"alerts": [], "run_at": None, "overall_status": "unknown"}

    all_alerts: list[dict] = []
    for check_name, check_data in (run.check_results or {}).items():
        for alert in check_data.get("alerts", []):
            all_alerts.append({**alert, "check": check_name})

    return {
        "run_at": run.run_at.isoformat(),
        "overall_status": run.overall_status,
        "alert_count": len(all_alerts),
        "alerts": all_alerts,
    }


# ── POST /heartbeat/send ──────────────────────────────────────────────────────

@router.post("/heartbeat/send", summary="Manually trigger a Telegram heartbeat")
def trigger_heartbeat(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Manually trigger the Telegram heartbeat summary message."""
    from app.watchdog.service import WatchdogService

    svc = WatchdogService(db)
    sent = svc.heartbeat()
    return {"sent": sent, "triggered_at": datetime.now(tz=timezone.utc).isoformat()}


# ── GET /telegram-events ──────────────────────────────────────────────────────

@router.get("/telegram-events", summary="Recent Telegram publication events (from poupi-baby)")
def telegram_events(
    hours: int = Query(24, ge=1, le=168),
    status: str | None = Query(None, description="sent | failed | rate_limited | skipped"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return recent TelegramPublicationEvent records."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    query = db.query(TelegramPublicationEvent).filter(
        TelegramPublicationEvent.published_at >= since
    )
    if status:
        query = query.filter(TelegramPublicationEvent.status == status)

    events = query.order_by(TelegramPublicationEvent.published_at.desc()).limit(limit).all()

    # Summary stats
    all_in_window = db.query(TelegramPublicationEvent).filter(
        TelegramPublicationEvent.published_at >= since
    ).all()
    by_status: dict[str, int] = {}
    for e in all_in_window:
        by_status[e.status] = by_status.get(e.status, 0) + 1

    return {
        "window_hours": hours,
        "total": len(all_in_window),
        "by_status": by_status,
        "events": [
            {
                "id": e.id,
                "marketplace": e.marketplace,
                "status": e.status,
                "price": float(e.price) if e.price else None,
                "deal_score": e.deal_score,
                "published_at": e.published_at.isoformat(),
                "fail_reason": e.fail_reason,
            }
            for e in events
        ],
    }


# ════════════════════════════════════════════════════════════════════
# Phase 3 — Analytics endpoints
# ════════════════════════════════════════════════════════════════════

@router.get("/stats", summary="Global + per-service heal/incident counters (Phase 3)")
def watchdog_stats(
    hours: int = Query(168, ge=1, le=8760, description="Window in hours (default 7 days)"),
) -> dict[str, Any]:
    """Return cumulative counters, heal success rate, recovery rate and MTTR."""
    from app.auto_healing.analytics import HistoryReader, MetricsCollector
    collector = MetricsCollector()
    reader = HistoryReader()
    metrics = collector.read_global_metrics(window_hours=hours)
    result = metrics.to_dict()
    mttr_map = reader.compute_mttr(window_hours=hours)
    for svc, m_dict in result["by_service"].items():
        if svc in mttr_map:
            m_dict.update({
                "mttr_history_avg_seconds": mttr_map[svc]["avg_seconds"],
                "mttr_history_p95_seconds": mttr_map[svc].get("p95_seconds"),
                "mttr_history_count": mttr_map[svc]["count"],
            })
    return result


@router.get("/incidents", summary="Incident list with duration and heal outcomes (Phase 3)")
def watchdog_incidents(
    hours: int = Query(168, ge=1, le=8760),
    service: str | None = Query(None, description="Filter by service name"),
    outcome: str | None = Query(None, description="Filter: recovered | open | unresolved"),
) -> dict[str, Any]:
    """Return incidents reconstructed from the watchdog history file."""
    from app.auto_healing.analytics import HistoryReader
    reader = HistoryReader()
    incidents = reader.extract_incidents(window_hours=hours)
    if service:
        incidents = [i for i in incidents if i.service == service]
    if outcome:
        incidents = [i for i in incidents if i.outcome == outcome]
    mttr_map = reader.compute_mttr(window_hours=hours)
    return {
        "window_hours": hours,
        "total": len(incidents),
        "mttr_by_service": mttr_map,
        "incidents": [i.to_dict() for i in incidents],
    }


@router.get("/healers", summary="Per-healer attempt / success / failure breakdown (Phase 3)")
def watchdog_healers(
    hours: int = Query(168, ge=1, le=8760),
) -> dict[str, Any]:
    """Return aggregated statistics for each healer from history."""
    from app.auto_healing.analytics import HistoryReader
    reader = HistoryReader()
    stats = reader.healer_stats(window_hours=hours)
    total_attempts = sum(s["attempts"] for s in stats)
    total_recovered = sum(s["recovered"] for s in stats)
    return {
        "window_hours": hours,
        "total_attempts": total_attempts,
        "total_recovered": total_recovered,
        "global_success_rate": round(total_recovered / total_attempts, 3) if total_attempts > 0 else None,
        "healers": stats,
    }


@router.get("/daily-report", summary="Human-readable daily summary (Phase 3)")
def watchdog_daily_report(
    date: str | None = Query(None, description="ISO date YYYY-MM-DD (defaults to today UTC)"),
    fmt: str = Query("json", description="json | text"),
) -> Any:
    """Generate the daily operational report for a specific date."""
    import datetime as dt_mod

    from app.auto_healing.analytics import DailyReporter
    target_date = None
    if date:
        try:
            target_date = dt_mod.date.fromisoformat(date)
        except ValueError:
            return {"error": f"Invalid date format: {date!r}. Use YYYY-MM-DD."}
    reporter = DailyReporter()
    report = reporter.generate(target_date)
    if fmt == "text":
        return {"text": report.to_text()}
    return report.to_dict()


# ════════════════════════════════════════════════════════════════════
# Phase 4 — Reliability, Forecast, Anomalies
# ════════════════════════════════════════════════════════════════════

@router.get("/reliability", summary="0-100 reliability score per service (Phase 4)")
def watchdog_reliability(
    hours: int = Query(168, ge=1, le=8760, description="History window for scoring"),
) -> dict[str, Any]:
    """Return reliability score (0-100) and grade per service.

    Scoring: base 100; deductions for downtime (-40 max), incidents (-20 max),
    failed heals (-15 max), circuit opens (-10 max). Bonus +1 if cooldown active.
    Grade: A+ ≥ 98, A ≥ 90, B ≥ 75, C ≥ 60, D ≥ 45, F < 45
    """
    from app.auto_healing.reliability import ReliabilityScorer
    scorer = ReliabilityScorer(window_hours=hours)
    scores = scorer.score_all()
    return {
        "window_hours": hours,
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
        "scores": {svc: score.to_dict() for svc, score in sorted(scores.items())},
    }


@router.get("/forecast", summary="Disk/queue/memory threshold ETA (Phase 4)")
def watchdog_forecast(
    window_hours: float = Query(48.0, ge=1.0, le=168.0),
) -> dict[str, Any]:
    """Forecast when disk, queue backlog, or memory will cross critical thresholds.

    Uses OLS linear regression over stored time series.
    Requires ≥4 data points; returns status=insufficient_data otherwise.
    """
    from app.auto_healing.reliability import Forecaster
    return Forecaster().forecast(window_hours=window_hours).to_dict()


@router.get("/anomalies", summary="Z-score anomaly detection for metric spikes (Phase 4)")
def watchdog_anomalies(
    window_hours: float = Query(24.0, ge=1.0, le=168.0),
) -> dict[str, Any]:
    """Detect metric spikes using z-score analysis (threshold: 2.5σ).

    Covers: disk_spike, memory_spike, queue_spike, restart_spike.
    Requires ≥8 data points per series.
    """
    from app.auto_healing.reliability import AnomalyDetector
    anomalies = AnomalyDetector().detect_all(window_hours=window_hours)
    return {
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
        "window_hours": window_hours,
        "total": len(anomalies),
        "anomalies": [a.to_dict() for a in anomalies],
    }


# ════════════════════════════════════════════════════════════════════
# Phase 5 — Operational Intelligence
# ════════════════════════════════════════════════════════════════════

@router.get("/executive-summary", summary="Weekly executive report with risk, recommendations, rankings (Phase 5)")
def watchdog_executive_summary(
    hours: int = Query(168, ge=24, le=8760, description="History window (default 7d)"),
    fmt: str = Query("json", description="json | text | telegram_daily | telegram_weekly"),
) -> Any:
    """Generate a weekly executive report combining all operational signals.

    Includes: overall reliability score/grade, top incident causes (7d/30d),
    worst services ranking, healer effectiveness, risk by service,
    forecast risks, and prioritised recommendations.

    fmt=text       → human-readable plain text
    fmt=json       → structured dict
    fmt=telegram_daily  → compact Telegram digest
    fmt=telegram_weekly → full Telegram executive summary
    """
    from app.auto_healing.intelligence import ExecutiveReporter
    report = ExecutiveReporter().generate(window_hours=hours)
    if fmt == "text":
        return {"text": report.to_text()}
    if fmt in ("telegram_daily", "telegram_weekly"):
        mode = "daily" if fmt == "telegram_daily" else "weekly"
        return {"text": report.to_telegram(mode=mode)}
    return report.to_dict()


@router.get("/recommendations", summary="Rule-based actionable recommendations per service (Phase 5)")
def watchdog_recommendations(
    hours: int = Query(168, ge=24, le=8760, description="History window"),
    priority: str | None = Query(None, description="Filter: HIGH | MEDIUM | LOW"),
    service: str | None = Query(None, description="Filter by service name"),
) -> dict[str, Any]:
    """Return prioritised recommendations generated from operational data.

    Rules fire on hard thresholds — deterministic, no ML.
    Categories: healer, reliability, operational, risk, forecast.

    Examples:
      workers success_rate=33% → "investigar root cause"
      redis   success_rate=100% → "healer confiável"
      score < 60               → "investigar causa raiz do downtime"
      MTTR > 30 min            → "healer pode não resolver causa raiz"
    """
    from app.auto_healing.intelligence import RecommendationsEngine
    recs = RecommendationsEngine().generate(window_hours=hours)
    if priority:
        recs = [r for r in recs if r.priority == priority.upper()]
    if service:
        recs = [r for r in recs if r.service == service]
    by_priority: dict[str, list] = {"HIGH": [], "MEDIUM": [], "LOW": []}
    for r in recs:
        by_priority.setdefault(r.priority, []).append(r.to_dict())
    return {
        "window_hours": hours,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total": len(recs),
        "by_priority": by_priority,
        "recommendations": [r.to_dict() for r in recs],
    }


@router.get("/risk-score", summary="Risk level (LOW/MEDIUM/HIGH/CRITICAL) per service (Phase 5)")
def watchdog_risk_score(
    hours: int = Query(168, ge=24, le=8760, description="History window"),
    min_risk: str | None = Query(None, description="Filter: LOW | MEDIUM | HIGH | CRITICAL"),
) -> dict[str, Any]:
    """Return risk assessment per service.

    Risk score (0-100): reliability component (max 40) + incident volume (max 30)
    + MTTR component (max 20) + heal rate component (max 10).

    Thresholds: CRITICAL ≥ 75, HIGH ≥ 50, MEDIUM ≥ 25, LOW < 25.
    """
    from app.auto_healing.intelligence import (
        _RISK_THRESHOLDS,
        RISK_CRITICAL,
        RISK_HIGH,
        RISK_LOW,
        RISK_MEDIUM,
        RiskScorer,
    )
    assessments = RiskScorer().score_all(window_hours=hours)
    _risk_order = {RISK_CRITICAL: 0, RISK_HIGH: 1, RISK_MEDIUM: 2, RISK_LOW: 3}
    if min_risk:
        min_level = min_risk.upper()
        min_ord = _risk_order.get(min_level, 3)
        assessments = [a for a in assessments if _risk_order.get(a.risk, 3) <= min_ord]
    summary = {RISK_CRITICAL: 0, RISK_HIGH: 0, RISK_MEDIUM: 0, RISK_LOW: 0}
    for a in assessments:
        summary[a.risk] = summary.get(a.risk, 0) + 1
    overall = (
        min(assessments, key=lambda a: _risk_order.get(a.risk, 3)).risk
        if assessments else RISK_LOW
    )
    return {
        "window_hours": hours,
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
        "overall_risk": overall,
        "summary": summary,
        "thresholds": _RISK_THRESHOLDS,
        "services": [a.to_dict() for a in assessments],
    }

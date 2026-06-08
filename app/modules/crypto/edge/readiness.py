"""Phase 9 — Forward Validation Readiness Panel.

Transforms forward_shadow_signals into a statistical decision surface.

Provides per-horizon:
- Readiness score (BOOTSTRAP → STATISTICALLY_RELEVANT)
- Sample gates (n ≥ 10 / 30 / 100)
- Confidence intervals for WR, avg_return, PF
- Edge status (INSUFFICIENT_DATA / NO_EDGE / POSSIBLE_EDGE / EDGE_DETECTED)

Also produces a Telegram daily summary.

Rules: no strategy changes, no signal changes, observation only.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.modules.crypto.edge.forward_model import ForwardShadowSignal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

READINESS_BOOTSTRAP = "BOOTSTRAP"
READINESS_EARLY = "EARLY_SAMPLE"
READINESS_MODERATE = "MODERATE_SAMPLE"
READINESS_RELEVANT = "STATISTICALLY_RELEVANT"

EDGE_INSUFFICIENT = "INSUFFICIENT_DATA"
EDGE_NO_EDGE = "NO_EDGE"
EDGE_POSSIBLE = "POSSIBLE_EDGE"
EDGE_DETECTED = "EDGE_DETECTED"

_Z95 = 1.96  # 95 % confidence
_GATE_10 = 10
_GATE_30 = 30
_GATE_100 = 100

# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------


def _wilson_ci(n_success: int, n_total: int, z: float = _Z95) -> tuple[float, float]:
    """Wilson score confidence interval for a proportion (95 % by default)."""
    if n_total == 0:
        return (0.0, 1.0)
    p = n_success / n_total
    z2 = z * z
    denom = 1.0 + z2 / n_total
    center = (p + z2 / (2.0 * n_total)) / denom
    margin = (z / denom) * math.sqrt(
        p * (1.0 - p) / n_total + z2 / (4.0 * n_total * n_total)
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def _normal_ci(values: list[float], z: float = _Z95) -> tuple[float, float] | None:
    """Normal-approximation CI for a mean (95 % by default).

    Uses z instead of t — conservative for n ≥ 30, slightly
    under-covers for small n, which is acceptable for observational use.
    """
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    se = math.sqrt(variance / n)
    return (mean - z * se, mean + z * se)


def _pf_ci(
    wins: list[float], losses: list[float], z: float = _Z95
) -> tuple[float, float] | None:
    """Approximate 95 % CI for profit factor using delta method.

    PF = sum(wins) / abs(sum(losses)).
    Uses Fieller's theorem approximation.  Returns None if no wins or losses.
    """
    if not wins or not losses:
        return None
    w = sum(wins)
    loss_sum = abs(sum(losses))
    if loss_sum == 0:
        return None
    pf = w / loss_sum
    n_w = len(wins)
    n_l = len(losses)
    # variance of win sum
    var_w = sum((x - w / n_w) ** 2 for x in wins) * n_w if n_w > 1 else 0.0
    # variance of loss sum
    var_l = sum((x - loss_sum / n_l) ** 2 for x in losses) * n_l if n_l > 1 else 0.0
    # delta method: Var(PF) ≈ (PF/w)^2 * var_w + (PF/l)^2 * var_l
    var_pf = (pf / w) ** 2 * var_w + (pf / loss_sum) ** 2 * var_l if w > 0 else 0.0
    se = math.sqrt(var_pf)
    return (max(0.0, pf - z * se), pf + z * se)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _readiness_score(n: int) -> str:
    if n < _GATE_10:
        return READINESS_BOOTSTRAP
    if n < _GATE_30:
        return READINESS_EARLY
    if n < _GATE_100:
        return READINESS_MODERATE
    return READINESS_RELEVANT


def _gates(n: int) -> dict[str, bool]:
    return {
        "n_ge_10": n >= _GATE_10,
        "n_ge_30": n >= _GATE_30,
        "n_ge_100": n >= _GATE_100,
    }


def _edge_status(
    n: int,
    wr: float | None,
    pf: float | None,
    wr_ci: tuple[float, float] | None,
) -> str:
    """Classify edge strength based on sample size, WR and PF."""
    if n < _GATE_10 or wr is None:
        return EDGE_INSUFFICIENT

    # Clear failure
    if wr < 0.50:
        return EDGE_NO_EDGE
    if pf is not None and pf < 1.0:
        return EDGE_NO_EDGE

    # Strong detection: n ≥ 30, WR ≥ 55 %, CI lower > 0.45, PF ≥ 1.5
    if (
        n >= _GATE_30
        and wr >= 0.55
        and (pf is None or pf >= 1.5)
        and wr_ci is not None
        and wr_ci[0] >= 0.45
    ):
        return EDGE_DETECTED

    # Weak / early: WR ≥ 50 % but not enough to declare DETECTED
    return EDGE_POSSIBLE


# ---------------------------------------------------------------------------
# Per-horizon readiness block
# ---------------------------------------------------------------------------


def _horizon_readiness(rows: list[ForwardShadowSignal], h: int) -> dict:
    """Full readiness analysis for one horizon h (24 | 72 | 168)."""
    evaluated = [s for s in rows if getattr(s, f"outcome_correct_{h}h") is not None]
    n_total = len(rows)
    n_eval = len(evaluated)

    returns = [
        float(getattr(s, f"return_{h}h"))
        for s in evaluated
        if getattr(s, f"return_{h}h") is not None
    ]
    n_correct = sum(1 for s in evaluated if getattr(s, f"outcome_correct_{h}h") is True)
    n_wrong = n_eval - n_correct

    # ---------- point estimates ----------
    wr = n_correct / n_eval if n_eval > 0 else None
    avg_r = sum(returns) / len(returns) if returns else None
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    pf: float | None = None
    if wins and losses:
        pf = sum(wins) / abs(sum(losses))
    elif wins and not losses:
        pf = None  # all wins — infinity, left as None

    # ---------- confidence intervals ----------
    wr_ci: tuple[float, float] | None = None
    avg_r_ci: tuple[float, float] | None = None
    pf_ci_val: tuple[float, float] | None = None

    if n_eval >= 2:
        wr_ci = _wilson_ci(n_correct, n_eval)
    if len(returns) >= 2:
        avg_r_ci = _normal_ci(returns)
    if wins and losses:
        pf_ci_val = _pf_ci(wins, losses)

    # ---------- classifications ----------
    readiness = _readiness_score(n_eval)
    gates = _gates(n_eval)
    edge = _edge_status(n_eval, wr, pf, wr_ci)

    return {
        "n_total": n_total,
        "n_evaluated": n_eval,
        "n_pending": n_total - n_eval,
        "readiness_score": readiness,
        "gates": gates,
        "win_rate": round(wr, 4) if wr is not None else None,
        "win_rate_ci_95": (
            (round(wr_ci[0], 4), round(wr_ci[1], 4)) if wr_ci is not None else None
        ),
        "avg_return_pct": round(avg_r, 4) if avg_r is not None else None,
        "avg_return_ci_95": (
            (round(avg_r_ci[0], 4), round(avg_r_ci[1], 4))
            if avg_r_ci is not None
            else None
        ),
        "profit_factor": round(pf, 4) if pf is not None else None,
        "profit_factor_ci_95": (
            (round(pf_ci_val[0], 4), round(pf_ci_val[1], 4))
            if pf_ci_val is not None
            else None
        ),
        "n_wins": n_correct,
        "n_losses": n_wrong,
        "edge_status": edge,
    }


# ---------------------------------------------------------------------------
# Overall GO/NO-GO
# ---------------------------------------------------------------------------

_HORIZON_WEIGHTS = {"24h": 0.2, "72h": 0.5, "168h": 0.3}


def _overall_verdict(horizon_blocks: dict) -> str:
    """Weighted GO/NO-GO across horizons.

    NO_EDGE on any horizon triggers NO_GO.
    All INSUFFICIENT_DATA → INSUFFICIENT_DATA.
    EDGE_DETECTED on primary (72h) with no NO_EDGE → GO.
    Otherwise → WATCH (promising but not confirmed).
    """
    statuses = {k: v["edge_status"] for k, v in horizon_blocks.items()}

    if all(s == EDGE_INSUFFICIENT for s in statuses.values()):
        return "INSUFFICIENT_DATA"
    if any(s == EDGE_NO_EDGE for s in statuses.values()):
        return "NO_GO"
    primary = statuses.get("72h", EDGE_INSUFFICIENT)
    if primary == EDGE_DETECTED:
        return "GO"
    if any(s in (EDGE_POSSIBLE, EDGE_DETECTED) for s in statuses.values()):
        return "WATCH"
    return "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------


def build_readiness_report(db: Session) -> dict:
    """Build Phase 9 readiness report for all forward shadow signals."""
    rows = (
        db.query(ForwardShadowSignal)
        .order_by(ForwardShadowSignal.signal_at)
        .all()
    )

    horizons: dict = {}
    for h in [24, 72, 168]:
        horizons[f"{h}h"] = _horizon_readiness(rows, h)

    verdict = _overall_verdict(horizons)
    overall_readiness = _readiness_score(len(rows))

    # Edge summary line
    edge_by_h = {k: v["edge_status"] for k, v in horizons.items()}

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "n_signals_tracked": len(rows),
        "overall_readiness": overall_readiness,
        "overall_verdict": verdict,
        "edge_by_horizon": edge_by_h,
        "horizons": horizons,
        "filter": {
            "regime": "UNKNOWN",
            "confidence_min": 75,
            "confidence_max": 84,
            "signal": "BUY",
            "note": "Observation only — no trades, no strategy changes",
        },
    }


# ---------------------------------------------------------------------------
# Daily Telegram summary
# ---------------------------------------------------------------------------


def _send_telegram(text: str) -> bool:
    """Send Telegram message. Returns True on success."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("1", "true", "yes")
    if not token or not chat_id or not enabled:
        logger.debug("readiness: telegram not configured / disabled, skipping")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10.0,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.warning("readiness: telegram returned %d", resp.status_code)
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness: telegram send failed: %s", exc)
        return False


def _edge_icon(status: str) -> str:
    return {
        EDGE_DETECTED: "✅",
        EDGE_POSSIBLE: "⚠️",
        EDGE_NO_EDGE: "❌",
        EDGE_INSUFFICIENT: "🔄",
    }.get(status, "❓")


def _readiness_icon(score: str) -> str:
    return {
        READINESS_BOOTSTRAP: "🔄",
        READINESS_EARLY: "📊",
        READINESS_MODERATE: "📈",
        READINESS_RELEVANT: "🏆",
    }.get(score, "❓")


def _verdict_icon(verdict: str) -> str:
    return {
        "GO": "🟢",
        "WATCH": "🟡",
        "NO_GO": "🔴",
        "INSUFFICIENT_DATA": "⚫",
    }.get(verdict, "❓")


def build_daily_summary_message(report: dict) -> str:
    """Format the daily Telegram summary from a readiness report."""
    n = report["n_signals_tracked"]
    verdict = report["overall_verdict"]
    readiness = report["overall_readiness"]
    horizons = report["horizons"]

    lines = [
        "<b>[Shadow Forward] Resumo Diário</b>",
        f"{_verdict_icon(verdict)} Verdict: <b>{verdict}</b>  "
        f"{_readiness_icon(readiness)} Readiness: <b>{readiness}</b>",
        f"N acumulado: <b>{n}</b> sinais (UNKNOWN + conf 75-84)",
        "",
    ]

    for label, hdata in horizons.items():
        ne = hdata["n_evaluated"]
        wr = hdata["win_rate"]
        pf = hdata["profit_factor"]
        edge = hdata["edge_status"]
        wr_ci = hdata["win_rate_ci_95"]
        icon = _edge_icon(edge)

        wr_str = f"{wr:.1%}" if wr is not None else "N/A"
        pf_str = f"{pf:.2f}" if pf is not None else "N/A"
        ci_str = (
            f"[{wr_ci[0]:.1%}, {wr_ci[1]:.1%}]" if wr_ci is not None else "N/A"
        )
        gates = hdata["gates"]
        gate_str = (
            f"{'✅' if gates['n_ge_10'] else '⬜'}10 "
            f"{'✅' if gates['n_ge_30'] else '⬜'}30 "
            f"{'✅' if gates['n_ge_100'] else '⬜'}100"
        )

        lines += [
            f"<b>{label}</b> {icon} {edge}",
            f"  N={ne} | WR={wr_str} CI={ci_str} | PF={pf_str}",
            f"  Gates: {gate_str}",
            "",
        ]

    lines += [
        "Regra: sem trades · apenas observação",
    ]
    return "\n".join(lines)


def send_daily_summary(db: Session) -> dict:
    """Build and send daily Telegram summary. Returns send metadata."""
    report = build_readiness_report(db)
    msg = build_daily_summary_message(report)
    sent = _send_telegram(msg)
    return {
        "sent": sent,
        "message_preview": msg[:200],
        "overall_verdict": report["overall_verdict"],
        "overall_readiness": report["overall_readiness"],
        "n_signals_tracked": report["n_signals_tracked"],
    }

"""Phase 8 — Forward Shadow Validation.

Tracks BUY signals matching regime=UNKNOWN AND confidence 75-84 going forward.
No trades. No strategy changes. Observation only.

Metrics collected per horizon (24h / 72h / 168h):
  N, WR, avg_return, PF, Sharpe

Alerts fired when:
  WR < 55%  |  PF < 1.5  |  avg_return < 0
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.modules.crypto.edge.calculator import _sharpe
from app.modules.crypto.edge.forward_model import ForwardShadowSignal

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORWARD_HORIZONS: list[int] = [24, 72, 168]
FORWARD_REGIME: str = "UNKNOWN"
FORWARD_CONF_MIN: int = 75
FORWARD_CONF_MAX: int = 84

ALERT_WR_MIN: float = 0.55
ALERT_PF_MIN: float = 1.5

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_div(a: float, b: float) -> float | None:
    if b == 0:
        return None
    return a / b


def _send_telegram(text: str) -> None:
    """Fire-and-forget Telegram message. Never raises."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("1", "true", "yes")
    if not token or not chat_id or not enabled:
        logger.debug("forward_shadow: telegram not configured / disabled, skipping")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("forward_shadow: telegram returned %d", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("forward_shadow: telegram send failed: %s", exc)


def _compute_horizon_outcome(
    db: Session,
    shadow: ForwardShadowSignal,
    horizon_hours: int,
    now: datetime,
) -> bool:
    """Compute and write outcome for one horizon.  Returns True if written."""
    from app.analytics.models import TradingAnalytics
    from app.normalization.models import NormalizedMarketCandle

    if shadow.analytics_id is None:
        return False

    analytics = db.get(TradingAnalytics, shadow.analytics_id)
    if analytics is None or analytics.market_candle_id is None:
        return False

    signal_candle = db.get(NormalizedMarketCandle, analytics.market_candle_id)
    if signal_candle is None:
        return False

    signal_ts: datetime = signal_candle.timestamp
    if signal_ts.tzinfo is None:
        signal_ts = signal_ts.replace(tzinfo=timezone.utc)
    horizon_deadline = signal_ts + timedelta(hours=horizon_hours)

    if now < horizon_deadline:
        # Horizon not yet closed
        return False

    future_candles = (
        db.query(NormalizedMarketCandle)
        .filter(
            NormalizedMarketCandle.symbol == analytics.symbol,
            NormalizedMarketCandle.timeframe == analytics.timeframe,
            NormalizedMarketCandle.timestamp > signal_ts,
            NormalizedMarketCandle.timestamp <= horizon_deadline,
        )
        .order_by(NormalizedMarketCandle.timestamp)
        .limit(horizon_hours + 10)
        .all()
    )

    if not future_candles:
        return False

    entry_price = float(signal_candle.close)
    exit_candle = future_candles[-1]
    exit_price = float(exit_candle.close)

    price_change_pct = ((exit_price - entry_price) / entry_price) * 100.0
    mfe = max((float(c.high) - entry_price) / entry_price * 100.0 for c in future_candles)
    mae = min((float(c.low) - entry_price) / entry_price * 100.0 for c in future_candles)
    outcome_correct = price_change_pct > 0

    h = horizon_hours
    setattr(shadow, f"return_{h}h", Decimal(str(round(price_change_pct, 4))))
    setattr(shadow, f"mfe_{h}h", Decimal(str(round(mfe, 4))))
    setattr(shadow, f"mae_{h}h", Decimal(str(round(mae, 4))))
    setattr(shadow, f"outcome_correct_{h}h", outcome_correct)
    setattr(shadow, f"outcome_at_{h}h", exit_candle.timestamp)
    shadow.updated_at = _now_utc()
    return True


# ---------------------------------------------------------------------------
# ForwardShadowTracker
# ---------------------------------------------------------------------------


class ForwardShadowTracker:
    """Idempotent tracker for forward shadow validation signals."""

    def run(self, db: Session) -> dict:
        captured = self._capture_new_signals(db)
        computed = self._compute_pending_outcomes(db)
        alerts = self._check_and_send_alerts(db)
        return {
            "captured_new": captured,
            "outcomes_computed": computed,
            "alerts_sent": len(alerts),
            "alert_details": alerts,
        }

    # ------------------------------------------------------------------
    # Step 1 — Capture new qualifying signals
    # ------------------------------------------------------------------

    def _capture_new_signals(self, db: Session) -> int:
        """Insert rows for new BUY signals not yet tracked.  Idempotent."""
        from app.analytics.models import TradingAnalytics

        # Analytics IDs already tracked
        existing_ids: set = {
            str(row.analytics_id)
            for row in db.query(ForwardShadowSignal).all()
            if row.analytics_id is not None
        }

        candidates = (
            db.query(TradingAnalytics)
            .filter(
                TradingAnalytics.signal == "BUY",
                TradingAnalytics.regime == FORWARD_REGIME,
                TradingAnalytics.confidence >= FORWARD_CONF_MIN,
                TradingAnalytics.confidence <= FORWARD_CONF_MAX,
            )
            .order_by(desc(TradingAnalytics.calculated_at))
            .limit(500)
            .all()
        )

        captured = 0
        for analytics in candidates:
            if str(analytics.id) in existing_ids:
                continue
            shadow = ForwardShadowSignal(
                analytics_id=analytics.id,
                symbol=analytics.symbol,
                timeframe=analytics.timeframe,
                confidence=analytics.confidence,
                regime=analytics.regime,
                signal_at=analytics.calculated_at,
            )
            # Try to get signal price from candle
            if analytics.market_candle_id is not None:
                try:
                    from app.normalization.models import NormalizedMarketCandle

                    candle = db.get(NormalizedMarketCandle, analytics.market_candle_id)
                    if candle is not None:
                        shadow.signal_price = candle.close
                        if analytics.calculated_at is None and candle.timestamp is not None:
                            shadow.signal_at = candle.timestamp
                except Exception as exc:  # noqa: BLE001
                    logger.debug("forward_shadow: candle lookup failed: %s", exc)
            db.add(shadow)
            captured += 1

        if captured:
            db.commit()
            logger.info("forward_shadow: captured %d new signals", captured)

        return captured

    # ------------------------------------------------------------------
    # Step 2 — Compute outcomes for closed horizons
    # ------------------------------------------------------------------

    def _compute_pending_outcomes(self, db: Session) -> int:
        """Compute outcomes for signals with pending (null) horizon results."""
        now = _now_utc()
        pending = db.query(ForwardShadowSignal).all()
        computed = 0

        for shadow in pending:
            changed = False
            for h in FORWARD_HORIZONS:
                already_done = getattr(shadow, f"outcome_correct_{h}h") is not None
                if already_done:
                    continue
                wrote = _compute_horizon_outcome(db, shadow, h, now)
                if wrote:
                    changed = True
                    computed += 1
            if changed:
                db.add(shadow)

        if computed:
            db.commit()
            logger.info("forward_shadow: computed %d horizon outcomes", computed)

        return computed

    # ------------------------------------------------------------------
    # Step 3 — Check metrics and send alerts
    # ------------------------------------------------------------------

    def _check_and_send_alerts(self, db: Session) -> list[str]:
        """Send Telegram for: new entry signals, degraded metrics per horizon."""
        sent: list[str] = []

        # 3a — Entry alerts for newly captured signals
        new_entries = (
            db.query(ForwardShadowSignal)
            .filter(ForwardShadowSignal.alert_entry_sent.is_(False))
            .all()
        )
        for shadow in new_entries:
            msg = (
                "<b>[Shadow Forward] Novo sinal capturado</b>\n"
                f"Symbol: {shadow.symbol} | TF: {shadow.timeframe}\n"
                f"Regime: {shadow.regime} | Conf: {shadow.confidence}\n"
                f"Signal at: {shadow.signal_at}"
            )
            _send_telegram(msg)
            shadow.alert_entry_sent = True
            shadow.updated_at = _now_utc()
            db.add(shadow)
            sent.append(f"entry:{shadow.symbol}:{shadow.timeframe}")

        if new_entries:
            db.commit()

        # 3b — Horizon outcome alerts + degraded metrics
        for h in FORWARD_HORIZONS:
            col_sent = f"alert_{h}h_sent"
            # Rows with a new outcome but alert not yet sent
            newly_computed = [
                s
                for s in db.query(ForwardShadowSignal).all()
                if getattr(s, f"outcome_correct_{h}h") is not None
                and not getattr(s, col_sent)
            ]
            if not newly_computed:
                continue

            # Compute accumulated metrics for this horizon
            all_with_outcome = [
                s
                for s in db.query(ForwardShadowSignal).all()
                if getattr(s, f"outcome_correct_{h}h") is not None
            ]
            returns = [
                float(getattr(s, f"return_{h}h"))
                for s in all_with_outcome
                if getattr(s, f"return_{h}h") is not None
            ]
            correct = sum(1 for s in all_with_outcome if getattr(s, f"outcome_correct_{h}h"))
            n = len(all_with_outcome)
            wr = correct / n if n else None
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r < 0]
            pf = _safe_div(sum(wins), abs(sum(losses))) if wins and losses else None
            avg_r = sum(returns) / len(returns) if returns else None

            # Build alert flags
            alert_flags: list[str] = []
            if wr is not None and wr < ALERT_WR_MIN:
                alert_flags.append(f"WR={wr:.1%} < {ALERT_WR_MIN:.0%}")
            if pf is not None and pf < ALERT_PF_MIN:
                alert_flags.append(f"PF={pf:.2f} < {ALERT_PF_MIN}")
            if avg_r is not None and avg_r < 0:
                alert_flags.append(f"avg_return={avg_r:.2f}% < 0")

            flag_line = " | ".join(alert_flags) if alert_flags else "OK"
            status = "ALERTA" if alert_flags else "OK"
            wr_str = f"{wr:.1%}" if wr is not None else "N/A"
            pf_str = f"{pf:.2f}" if pf is not None else "N/A"
            avg_str = f"{avg_r:.2f}" if avg_r is not None else "N/A"
            msg = (
                f"<b>[Shadow Forward {h}h] Outcome acumulado — {status}</b>\n"
                f"N={n} | WR={wr_str} | PF={pf_str} | "
                f"avg_return={avg_str}%\n"
                f"Flags: {flag_line}"
            )
            _send_telegram(msg)

            for shadow in newly_computed:
                setattr(shadow, col_sent, True)
                shadow.updated_at = _now_utc()
                db.add(shadow)

            db.commit()
            sent.append(f"horizon_{h}h:n={n}:flags={flag_line}")

        return sent


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _horizon_metrics(rows: list[ForwardShadowSignal], h: int) -> dict:
    """Compute metrics dict for a given horizon across all rows."""
    evaluated = [s for s in rows if getattr(s, f"outcome_correct_{h}h") is not None]
    n_total = len(rows)
    n_eval = len(evaluated)
    n_pending = n_total - n_eval

    if n_eval == 0:
        return {
            "n_total": n_total,
            "n_evaluated": 0,
            "n_pending": n_pending,
            "win_rate": None,
            "avg_return_pct": None,
            "profit_factor": None,
            "sharpe": None,
            "alert_flags": [],
            "go_no_go": "INSUFFICIENT_DATA",
        }

    returns = [
        float(getattr(s, f"return_{h}h"))
        for s in evaluated
        if getattr(s, f"return_{h}h") is not None
    ]
    n_correct = sum(1 for s in evaluated if getattr(s, f"outcome_correct_{h}h") is True)
    wr = n_correct / n_eval
    avg_r = sum(returns) / len(returns) if returns else None
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    pf = _safe_div(sum(wins), abs(sum(losses))) if wins and losses else (None if not wins else None)
    sharpe = _sharpe(returns) if len(returns) >= 2 else None

    alert_flags: list[str] = []
    if wr < ALERT_WR_MIN:
        alert_flags.append("WR_BELOW_55")
    if pf is not None and pf < ALERT_PF_MIN:
        alert_flags.append("PF_BELOW_1.5")
    if avg_r is not None and avg_r < 0:
        alert_flags.append("AVG_RETURN_NEGATIVE")

    if n_eval < 5:
        go_no_go = "INSUFFICIENT_DATA"
    elif alert_flags:
        go_no_go = "NO_GO"
    else:
        go_no_go = "GO"

    return {
        "n_total": n_total,
        "n_evaluated": n_eval,
        "n_pending": n_pending,
        "win_rate": round(wr, 4),
        "avg_return_pct": round(avg_r, 4) if avg_r is not None else None,
        "profit_factor": round(pf, 4) if pf is not None else None,
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "alert_flags": alert_flags,
        "go_no_go": go_no_go,
    }


def _edge_stability_verdict(horizons: dict) -> str:
    """Aggregate GO/NO_GO across horizons."""
    verdicts = [v["go_no_go"] for v in horizons.values()]
    if all(v == "INSUFFICIENT_DATA" for v in verdicts):
        return "INSUFFICIENT_DATA"
    if any(v == "NO_GO" for v in verdicts):
        return "NO_GO"
    if all(v == "GO" for v in verdicts if v != "INSUFFICIENT_DATA"):
        return "GO"
    return "INCONCLUSIVE"


def build_forward_validation_report(db: Session, run_tracker: bool = True) -> dict:
    """Build full Phase 8 forward validation report.

    If *run_tracker* is True (default), also runs the tracker to capture new
    signals and compute pending outcomes before generating the report.
    """
    tracker_result: dict = {}
    if run_tracker:
        tracker = ForwardShadowTracker()
        tracker_result = tracker.run(db)

    rows = db.query(ForwardShadowSignal).order_by(desc(ForwardShadowSignal.signal_at)).all()

    horizons: dict = {}
    for h in FORWARD_HORIZONS:
        horizons[f"{h}h"] = _horizon_metrics(rows, h)

    edge_stability = _edge_stability_verdict(horizons)

    # All alert flags across all horizons
    all_flags = [flag for hm in horizons.values() for flag in hm["alert_flags"]]

    return {
        "generated_at": _now_utc().isoformat(),
        "filter": {
            "regime": FORWARD_REGIME,
            "confidence_min": FORWARD_CONF_MIN,
            "confidence_max": FORWARD_CONF_MAX,
            "signal": "BUY",
            "note": "Observation only — no trades, no strategy changes",
        },
        "n_signals_tracked": len(rows),
        "horizons": horizons,
        "edge_stability": edge_stability,
        "active_alerts": list(set(all_flags)),
        "tracker_run": tracker_result,
        "signals": [
            {
                "id": str(s.id),
                "analytics_id": str(s.analytics_id) if s.analytics_id else None,
                "symbol": s.symbol,
                "timeframe": s.timeframe,
                "confidence": s.confidence,
                "regime": s.regime,
                "signal_at": s.signal_at.isoformat() if s.signal_at else None,
                "signal_price": float(s.signal_price) if s.signal_price is not None else None,
                "return_24h": float(s.return_24h) if s.return_24h is not None else None,
                "outcome_correct_24h": s.outcome_correct_24h,
                "return_72h": float(s.return_72h) if s.return_72h is not None else None,
                "outcome_correct_72h": s.outcome_correct_72h,
                "return_168h": float(s.return_168h) if s.return_168h is not None else None,
                "outcome_correct_168h": s.outcome_correct_168h,
            }
            for s in rows
        ],
    }

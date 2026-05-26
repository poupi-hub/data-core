"""Dataset maturity assessment for the quant outcome pipeline.

Measures how "ready" the accumulated dataset is for quantitative validation.
A dataset that has just started collecting is in "bootstrap" phase; as more
outcomes accumulate and signal diversity increases, it advances toward
"calibration-ready".

Maturity score breakdown (0–100):
  - Volume          (0–30 pts): total evaluated outcomes
  - Signal diversity (0–25 pts): non-HOLD outcomes (actual directional signals)
  - Regime diversity (0–20 pts): distinct market regimes observed
  - Symbol diversity (0–15 pts): distinct symbols with at least one outcome
  - Confidence spread (0–10 pts): spread of confidence values (avoid monoculture)

Score bands:
   0–20   → BOOTSTRAP    (insufficient data — expected days 0–3)
  20–50   → IMMATURE     (accumulating — expected days 3–7)
  50–75   → USEFUL       (statistically useful — expected days 7–14)
  75–100  → CALIBRATION_READY (ready for calibration validation)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.modules.trading.validation.models import TradingSignalOutcome
from app.modules.trading.validation.pipeline_health import BOOTSTRAP_OUTCOME_THRESHOLD

logger = logging.getLogger(__name__)

# Calibration readiness thresholds (also used by /readiness endpoint).
CALIBRATION_MIN_OUTCOMES: int = 50
CALIBRATION_MIN_NON_HOLD: int = 10
CALIBRATION_MIN_REGIMES: int = 3
CALIBRATION_MIN_SYMBOLS: int = 3
DRIFT_MIN_OUTCOMES: int = 100   # need a stable baseline for drift detection

_metrics_loaded = False
_m_maturity: object = None


def _load_metrics() -> None:
    global _metrics_loaded, _m_maturity
    if _metrics_loaded:
        return
    try:
        from api.metrics import dataset_maturity_score  # noqa: PLC0415
        _m_maturity = dataset_maturity_score
        _metrics_loaded = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load Prometheus metrics (non-fatal): %s", exc)


@dataclass
class MaturityReport:
    """Dataset maturity report."""

    maturity_score: float          # 0–100
    band: str                      # BOOTSTRAP | IMMATURE | USEFUL | CALIBRATION_READY

    total_outcomes: int
    non_hold_outcomes: int         # BUY + SELL outcomes (excludes HOLD-derived)
    distinct_regimes: int
    distinct_symbols: int
    confidence_spread: float       # std-dev of confidence values (0 if none)

    calibration_ready: bool
    drift_ready: bool
    replay_ready: bool

    components: dict[str, Any] = field(default_factory=dict)


class DatasetMaturityService:
    """Assesses and scores the maturity of the quant outcome dataset.

    Call ``assess()`` from the dataset_quality_crypto_job to keep
    ``dataset_maturity_score`` Prometheus gauge current.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        _load_metrics()

    def assess(self) -> MaturityReport:
        """Run all maturity checks and emit Prometheus gauge."""
        total = self._count_total()
        non_hold = self._count_non_hold()
        regimes = self._count_distinct_regimes()
        symbols = self._count_distinct_symbols()
        conf_spread = self._confidence_spread()

        # ── Component scores ───────────────────────────────────────────────────
        vol_pts = min(30.0, round(30.0 * total / max(CALIBRATION_MIN_OUTCOMES, 1), 2))
        div_pts = min(25.0, round(25.0 * non_hold / max(CALIBRATION_MIN_NON_HOLD, 1), 2))
        reg_pts = min(20.0, round(20.0 * regimes / max(CALIBRATION_MIN_REGIMES, 1), 2))
        sym_pts = min(15.0, round(15.0 * symbols / max(CALIBRATION_MIN_SYMBOLS, 1), 2))
        # Confidence spread: full points when std_dev >= 15 (diverse confidence values)
        conf_pts = min(10.0, round(10.0 * min(conf_spread, 15.0) / 15.0, 2))

        maturity_score = round(vol_pts + div_pts + reg_pts + sym_pts + conf_pts, 2)

        band: str
        if maturity_score < 20:
            band = "BOOTSTRAP"
        elif maturity_score < 50:
            band = "IMMATURE"
        elif maturity_score < 75:
            band = "USEFUL"
        else:
            band = "CALIBRATION_READY"

        calibration_ready = (
            total >= CALIBRATION_MIN_OUTCOMES
            and non_hold >= CALIBRATION_MIN_NON_HOLD
            and regimes >= CALIBRATION_MIN_REGIMES
            and symbols >= CALIBRATION_MIN_SYMBOLS
        )
        drift_ready = total >= DRIFT_MIN_OUTCOMES
        replay_ready = total >= CALIBRATION_MIN_NON_HOLD  # minimal: at least some non-HOLD

        report = MaturityReport(
            maturity_score=maturity_score,
            band=band,
            total_outcomes=total,
            non_hold_outcomes=non_hold,
            distinct_regimes=regimes,
            distinct_symbols=symbols,
            confidence_spread=round(conf_spread, 2),
            calibration_ready=calibration_ready,
            drift_ready=drift_ready,
            replay_ready=replay_ready,
            components={
                "vol_pts": vol_pts,
                "div_pts": div_pts,
                "reg_pts": reg_pts,
                "sym_pts": sym_pts,
                "conf_pts": conf_pts,
                "thresholds": {
                    "calibration_min_outcomes": CALIBRATION_MIN_OUTCOMES,
                    "calibration_min_non_hold": CALIBRATION_MIN_NON_HOLD,
                    "calibration_min_regimes": CALIBRATION_MIN_REGIMES,
                    "calibration_min_symbols": CALIBRATION_MIN_SYMBOLS,
                    "drift_min_outcomes": DRIFT_MIN_OUTCOMES,
                },
            },
        )

        self._emit(maturity_score)
        return report

    # ── DB queries ─────────────────────────────────────────────────────────────

    def _count_total(self) -> int:
        return self.db.query(func.count(TradingSignalOutcome.id)).scalar() or 0

    def _count_non_hold(self) -> int:
        """Outcomes from BUY or SELL signals (not HOLD-derived)."""
        return (
            self.db.query(func.count(TradingSignalOutcome.id))
            .filter(TradingSignalOutcome.signal.in_(["BUY", "SELL"]))
            .scalar()
            or 0
        )

    def _count_distinct_regimes(self) -> int:
        return (
            self.db.query(func.count(func.distinct(TradingSignalOutcome.regime)))
            .filter(TradingSignalOutcome.regime.isnot(None))
            .scalar()
            or 0
        )

    def _count_distinct_symbols(self) -> int:
        return (
            self.db.query(func.count(func.distinct(TradingSignalOutcome.symbol)))
            .scalar()
            or 0
        )

    def _confidence_spread(self) -> float:
        """Return standard deviation of confidence values (0.0 if insufficient data)."""
        result = (
            self.db.query(func.stddev_pop(TradingSignalOutcome.confidence))
            .filter(TradingSignalOutcome.confidence.isnot(None))
            .scalar()
        )
        return float(result) if result is not None else 0.0

    # ── Prometheus emit ────────────────────────────────────────────────────────

    def _emit(self, score: float) -> None:
        if not _metrics_loaded:
            return
        try:
            _m_maturity.set(score)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Maturity score Prometheus emit failed (non-fatal): %s", exc)

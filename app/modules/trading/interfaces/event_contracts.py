"""Typed event contracts for the trading signal pipeline.

These TypedDicts define the canonical shape of events flowing between
data-core and downstream consumers (poupi-crypto, ML pipelines, audit logs).

Design notes
────────────
- No runtime dependencies: stdlib only.
- ``Literal`` types constrain the discriminator fields so that consumers can
  switch on ``event_type`` without stringly-typed comparisons.
- All timestamps are ISO-8601 strings (UTC) so the payload is JSON-serialisable
  without a custom encoder.
- ``analytics_id`` / ``outcome_id`` are string UUIDs to avoid uuid.UUID
  serialisation issues across service boundaries.
"""

from __future__ import annotations

from typing import Literal
from typing import TypedDict


class SignalEvent(TypedDict):
    """Emitted when trading_analytics produces a new BUY / SELL / HOLD signal."""

    event_type: Literal["signal_generated"]
    symbol: str           # e.g. "SOL/USDT"
    timeframe: str        # e.g. "1h"
    signal: str           # "BUY" | "SELL" | "HOLD"
    confidence: int       # 0-100
    regime: str           # e.g. "trending_up"
    price: float          # close price at signal candle
    timestamp: str        # ISO-8601 UTC candle timestamp
    analytics_id: str     # UUID of the TradingAnalytics row


class DecisionEvent(TypedDict):
    """Emitted when poupi-crypto acts on a SignalEvent (places / rejects order)."""

    event_type: Literal["decision_made"]
    analytics_id: str     # references SignalEvent.analytics_id
    symbol: str
    signal: str           # "BUY" | "SELL" | "HOLD"
    decision: str         # "EXECUTE" | "SKIP" | "HOLD_OVERRIDE"
    reason: str           # human-readable justification
    position_size: float  # fraction of portfolio allocated (0.0 if skipped)
    timestamp: str        # ISO-8601 UTC when the decision was made


class OutcomeEvent(TypedDict):
    """Emitted when SignalOutcomeTracker evaluates a past signal."""

    event_type: Literal["outcome_evaluated"]
    outcome_id: str           # UUID of the TradingSignalOutcome row
    analytics_id: str | None  # references SignalEvent.analytics_id (may be None)
    symbol: str
    timeframe: str
    signal: str               # "BUY" | "SELL"
    confidence: int
    signal_price: float
    outcome_price: float
    price_change_pct: float
    max_favorable_pct: float | None
    max_adverse_pct: float | None
    outcome_correct: bool
    candles_elapsed: int
    signal_at: str    # ISO-8601 UTC
    outcome_at: str   # ISO-8601 UTC
    evaluated_at: str # ISO-8601 UTC

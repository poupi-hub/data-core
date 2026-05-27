"""Adaptive Policy Contract — Prometheus metrics.

All metrics use the prefix ``adaptive_policy_`` to avoid collisions with
``optruth_``, ``adaptive_``, and ``enforcement_`` metric families.

Best-effort: publish functions never raise.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Counters ───────────────────────────────────────────────────────────────────

adaptive_policy_generated_total = Counter(
    "adaptive_policy_generated_total",
    "Total number of AdaptivePolicyContract instances generated",
    ["environment", "mode", "status"],  # labels: mode=OBSERVE_ONLY|WARN_ONLY|..., status=OK|WARNING|CRITICAL
)

adaptive_policy_boost_blocked_total = Counter(
    "adaptive_policy_boost_blocked_total",
    "Total number of times BOOST was blocked by ConfidenceSafetyValidator",
    ["reason_class"],  # labels: sample_size | profit_factor | calibration | replayability | quant | safe_mode
)

adaptive_policy_safe_mode_total = Counter(
    "adaptive_policy_safe_mode_total",
    "Total number of contracts issued with safe_mode=True",
)

adaptive_policy_fail_closed_total = Counter(
    "adaptive_policy_fail_closed_total",
    "Total number of contracts issued with fail_closed=True (FAIL_CLOSED mode)",
)

adaptive_policy_confidence_penalty_total = Counter(
    "adaptive_policy_confidence_penalty_total",
    "Total confidence points deducted by the safety validator",
    ["penalty_type"],  # labels: calibration | overconfidence | slope | replayability | quant
)

adaptive_policy_entropy_penalty_total = Counter(
    "adaptive_policy_entropy_penalty_total",
    "Total quant/entropy-driven confidence penalties applied",
)

adaptive_policy_fallback_total = Counter(
    "adaptive_policy_fallback_total",
    "Total number of times the generator returned the safe OBSERVE_ONLY fallback",
)

# ── Gauges — current state ─────────────────────────────────────────────────────

adaptive_policy_mode_gauge = Gauge(
    "adaptive_policy_mode",
    "Current adaptive policy mode encoded: 1=OBSERVE_ONLY 2=WARN_ONLY 3=SAFE_MODE 4=FAIL_CLOSED",
)

adaptive_policy_confidence_score_gauge = Gauge(
    "adaptive_policy_confidence_score",
    "Adjusted confidence score in the latest adaptive policy contract (0-100)",
)

adaptive_policy_replayability_score_gauge = Gauge(
    "adaptive_policy_replayability_score",
    "Replayability score reflected in the latest adaptive policy contract (0-100)",
)

adaptive_policy_quant_reliability_score_gauge = Gauge(
    "adaptive_policy_quant_reliability_score",
    "Quant reliability score reflected in the latest adaptive policy contract (0-100)",
)

adaptive_policy_boost_blocked_gauge = Gauge(
    "adaptive_policy_boost_blocked_state",
    "1 if BOOST is currently blocked by the safety validator, 0 otherwise",
)

adaptive_policy_safe_mode_gauge = Gauge(
    "adaptive_policy_safe_mode_state",
    "1 if the latest contract has safe_mode=True, 0 otherwise",
)

adaptive_policy_fail_closed_gauge = Gauge(
    "adaptive_policy_fail_closed_state",
    "1 if the latest contract has fail_closed=True, 0 otherwise",
)

adaptive_policy_rollout_phase_gauge = Gauge(
    "adaptive_policy_rollout_phase",
    "Current rollout phase (1-4)",
)

# ── Histogram ─────────────────────────────────────────────────────────────────

adaptive_policy_generation_duration_seconds = Histogram(
    "adaptive_policy_generation_duration_seconds",
    "Wall-clock duration of a full AdaptivePolicyContract generation",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)

# ── Helpers ────────────────────────────────────────────────────────────────────

_MODE_ENCODING = {
    "OBSERVE_ONLY": 1,
    "WARN_ONLY":    2,
    "SAFE_MODE":    3,
    "FAIL_CLOSED":  4,
}

# Maps block_reason text patterns → counter label
_REASON_CLASS_MAP: list[tuple[str, str]] = [
    ("sample_size",    "sample_size"),
    ("profit_factor",  "profit_factor"),
    ("calibrat",       "calibration"),
    ("replayability",  "replayability"),
    ("quant_reliab",   "quant"),
    ("entropy",        "quant"),
    ("safe_mode",      "safe_mode"),
    ("slope",          "slope"),
    ("overconfidenc",  "overconfidence"),
]


def _classify_reason(reason: str) -> str:
    lower = reason.lower()
    for fragment, label in _REASON_CLASS_MAP:
        if fragment in lower:
            return label
    return "other"


def publish_contract(contract: "AdaptivePolicyContract") -> None:  # type: ignore[name-defined]  # noqa: F821
    """Push all gauge/counter updates from a completed contract. Never raises."""
    try:
        mode = contract.mode
        status = contract.status
        env = contract.environment

        # Counters
        adaptive_policy_generated_total.labels(
            environment=env, mode=mode, status=status
        ).inc()

        if contract.safe_mode:
            adaptive_policy_safe_mode_total.inc()

        if contract.fail_closed:
            adaptive_policy_fail_closed_total.inc()

        if contract.boost_blocked:
            for reason in contract.boost_block_reasons:
                label = _classify_reason(reason)
                adaptive_policy_boost_blocked_total.labels(reason_class=label).inc()

        if contract.confidence_penalty_applied > 0:
            # We can't retrospectively label the penalty type from the contract,
            # so we record the total as a single increment.
            adaptive_policy_confidence_penalty_total.labels(
                penalty_type="total"
            ).inc(contract.confidence_penalty_applied)

        if contract.quant_reliability_score < 60:
            adaptive_policy_entropy_penalty_total.inc(
                max(0, contract.uncertainty_penalty_applied)
            )

        # Gauges
        adaptive_policy_mode_gauge.set(_MODE_ENCODING.get(mode, 0))
        adaptive_policy_confidence_score_gauge.set(contract.confidence_score)
        adaptive_policy_replayability_score_gauge.set(contract.replayability_score)
        adaptive_policy_quant_reliability_score_gauge.set(contract.quant_reliability_score)
        adaptive_policy_boost_blocked_gauge.set(1 if contract.boost_blocked else 0)
        adaptive_policy_safe_mode_gauge.set(1 if contract.safe_mode else 0)
        adaptive_policy_fail_closed_gauge.set(1 if contract.fail_closed else 0)
        adaptive_policy_rollout_phase_gauge.set(contract.rollout_phase)

    except Exception:
        pass  # metric publish never kills the caller

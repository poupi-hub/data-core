"""
live_metrics.py — Phase Q Q-10

Prometheus metrics for Phase Q: Micro-Live Execution & Capital-Protected Autonomy.

Métricas que já existem em api/metrics.py (Phase P) são importadas daqui —
NÃO redefinidas — para evitar "Duplicated timeseries in CollectorRegistry".

All metrics are optional — Phase Q modules wrap their import in try/except.

Metrics owned here (novas, Phase Q):
  live_governance_score          Gauge
  divergence_score               Gauge
  live_consistency_score         Gauge
  guardian_emergency_level       Gauge
  contraction_multiplier         Gauge
  exchange_instability_score     Gauge
  live_drawdown_pct              Gauge
  live_capital_exposure_pct      Gauge
  execution_latency_ms           Gauge
  live_slippage_bps              Gauge
  autonomous_freeze_state        Gauge
  rollback_events_total          Counter[trigger]

Metrics re-exported from api/metrics.py (Phase P — already registered):
  execution_quality_score        → api.metrics.execution_quality_score (Phase O)
  live_readiness_score           → api.metrics.live_readiness_score    (Phase P)
"""

from prometheus_client import Gauge, Counter

# ── Phase Q — novas métricas (não existem em api/metrics.py) ──────────────────

live_governance_score = Gauge(
    "live_governance_score",
    "Overall live governance health score (0-100) — AutonomousLiveGovernance",
)

live_slippage_bps = Gauge(
    "live_slippage_bps",
    "Average live slippage in basis points — LiveExecutionAuditor",
)

execution_latency_ms = Gauge(
    "execution_latency_ms",
    "Average live execution latency in milliseconds — LiveExecutionAuditor",
)

divergence_score = Gauge(
    "divergence_score",
    "Paper vs live divergence score (0-100, 100=worst) — PaperVsLiveDivergenceEngine",
)

live_consistency_score = Gauge(
    "live_consistency_score",
    "Live consistency with paper trading (0-100) — PaperVsLiveDivergenceEngine",
)

guardian_emergency_level = Gauge(
    "guardian_emergency_level",
    "Guardian emergency level (0=normal, 5=shutdown) — AutonomousLiveGuardian",
)

contraction_multiplier = Gauge(
    "contraction_multiplier",
    "Current position sizing contraction multiplier (0.0-1.0) — AutonomousLiveGuardian",
)

exchange_instability_score = Gauge(
    "exchange_instability_score",
    "Exchange instability score (0-100) — AutonomousLiveGuardian",
)

live_drawdown_pct = Gauge(
    "live_drawdown_pct",
    "Current live daily drawdown as fraction of total capital — LiveCapitalPreservationEngine",
)

live_capital_exposure_pct = Gauge(
    "live_capital_exposure_pct",
    "Current live capital exposure as fraction of total capital — LiveCapitalPreservationEngine",
)

autonomous_freeze_state = Gauge(
    "autonomous_freeze_state",
    "1 if live execution is frozen/blocked, 0 if normal — MicroLiveExecutionController",
)

# Phase Q usa nome distinto para não colidir com live_readiness_score da Phase P
# (api/metrics.py define live_readiness_score = MicroLiveReadinessEngine gate score)
continuous_live_readiness_score = Gauge(
    "continuous_live_readiness_score",
    "Continuous live readiness score during operation (0-100) — LiveReadinessRevalidationEngine",
)

execution_quality_score = Gauge(
    "execution_quality_score",
    "Live execution quality score (0-100) — LiveExecutionAuditor",
)

rollback_events_total = Counter(
    "rollback_events_total",
    "Total autonomous rollback events — AutonomousRollbackEngine",
    ["trigger"],
)

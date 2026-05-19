"""
burnin_metrics.py — Phase S S-10

Prometheus metrics for the Phase S burn-in and observability validation layer.
29 Gauges + 4 Counters = 33 metrics total.

NOTE: burnin_operational_maturity_score uses the "burnin_" prefix to avoid
collision with Phase R's operational_maturity_score defined in runtime_metrics.py.

Usage:
    from api.burnin_metrics import burnin_stability_score
    burnin_stability_score.set(85.3)
"""

from prometheus_client import Gauge, Counter

# ── S-1: Runtime Burn-In Engine ───────────────────────────────────────────
burnin_stability_score = Gauge(
    "burnin_stability_score",
    "Overall burn-in stability score (0-100). Weighted average across 8 burn-in dimensions.",
)
runtime_burnin_score = Gauge(
    "runtime_burnin_score",
    "Runtime burn-in score reflecting uptime phase maturity and restart frequency.",
)
long_session_integrity_score = Gauge(
    "long_session_integrity_score",
    "Integrity of long-running session continuity, penalising runtime decay.",
)
burnin_uptime_hours = Gauge(
    "burnin_uptime_hours",
    "Current estimated uptime in hours from runtime_burnin_log.",
)

# ── S-2: Metrics Integrity Validator ─────────────────────────────────────
metrics_integrity_score = Gauge(
    "metrics_integrity_score",
    "Percentage of Prometheus metrics with fresh JSONL source files.",
)
metrics_continuity_score = Gauge(
    "metrics_continuity_score",
    "Weighted continuity score: fresh=1.0 + stale=0.5 / total.",
)
observability_health_score = Gauge(
    "observability_health_score",
    "Composite observability health: integrity*0.6 + importer_health*0.4.",
)

# ── S-3: Grafana Dashboard Validator ─────────────────────────────────────
dashboard_integrity_score = Gauge(
    "dashboard_integrity_score",
    "Percentage of expected Grafana dashboards that are structurally valid.",
)
panel_health_score = Gauge(
    "panel_health_score",
    "Percentage of dashboard panels with valid expr, datasource, and gridPos.",
)
visualization_consistency_score = Gauge(
    "visualization_consistency_score",
    "Consistency score penalising missing and corrupt dashboard files.",
)

# ── S-4: Collector Reliability Engine ────────────────────────────────────
collector_reliability_score = Gauge(
    "collector_reliability_score",
    "Percentage of JSONL collectors classified as fresh or recent (<6h).",
)
normalization_integrity_score = Gauge(
    "normalization_integrity_score",
    "JSONL parse integrity: 1 - (parse_errors / total_lines) * 100.",
)
data_freshness_score = Gauge(
    "data_freshness_score",
    "Weighted freshness score across all collectors (fresh=1.0, recent=0.7, stale=0.3).",
)

# ── S-5: Replay Integrity Burn-In Validator ───────────────────────────────
replay_burnin_score = Gauge(
    "replay_burnin_score",
    "Replay session health score weighted by status (healthy/degraded/corrupt/missing).",
)
replay_continuity_score = Gauge(
    "replay_continuity_score",
    "Replay continuity after penalising temporal gaps >30 min.",
)
replay_consistency_score = Gauge(
    "replay_consistency_score",
    "Replay consistency penalising JSONL parse errors across replay sessions.",
)

# ── S-6: Incident Noise Reduction Engine ─────────────────────────────────
incident_signal_quality_score = Gauge(
    "incident_signal_quality_score",
    "Percentage of non-noisy incidents relative to total (1 - noisy/total) * 100.",
)
alert_precision_score = Gauge(
    "alert_precision_score",
    "Alert precision after penalising duplicate and storm alert patterns.",
)
operational_noise_score = Gauge(
    "operational_noise_score",
    "Composite noise score: signal_quality*0.6 + alert_precision*0.4.",
)
burnin_noisy_subsystems = Gauge(
    "burnin_noisy_subsystems",
    "Number of subsystems with noise_ratio >= 0.20 in the last incident analysis.",
)

# ── S-7: Cold Start Resilience Validator ─────────────────────────────────
cold_start_resilience_score = Gauge(
    "cold_start_resilience_score",
    "Cold-start resilience score (0-100) from 10-step structural validation. Grade A=95+.",
)

# ── S-8: Operational Drift Analyzer ──────────────────────────────────────
operational_drift_score = Gauge(
    "operational_drift_score",
    "Average per-dimension drift stability score (100=no drift, 0=chaotic).",
)
runtime_consistency_trend = Gauge(
    "runtime_consistency_trend",
    "Runtime consistency trend: (stable + drifting*0.5) / active_dimensions * 100.",
)
stability_trend_score = Gauge(
    "stability_trend_score",
    "Stability trend score penalising dimensions classified as degrading.",
)
burnin_drift_dimensions_degrading = Gauge(
    "burnin_drift_dimensions_degrading",
    "Number of operational drift dimensions currently in degrading state.",
)

# ── S-9: Runtime Stability Orchestrator ──────────────────────────────────
runtime_stability_score = Gauge(
    "runtime_stability_score",
    "Overall Phase S runtime stability: equal-weighted average of all 8 phase scores.",
)
observability_readiness_score = Gauge(
    "observability_readiness_score",
    "Observability cluster readiness: S-2*0.4 + S-3*0.3 + S-4*0.3.",
)
burnin_readiness_score = Gauge(
    "burnin_readiness_score",
    "Burn-in cluster readiness: S-1*0.5 + S-5*0.3 + S-6*0.2.",
)
burnin_operational_maturity_score = Gauge(
    "burnin_operational_maturity_score",
    "Phase S operational maturity composite. Uses 'burnin_' prefix to avoid "
    "collision with Phase R operational_maturity_score.",
)

# ── Counters ──────────────────────────────────────────────────────────────
burnin_validations_total = Counter(
    "burnin_validations_total",
    "Total number of Phase S full validation cycles completed.",
)
burnin_failures_total = Counter(
    "burnin_failures_total",
    "Total number of Phase S validation phase failures.",
    ["phase_id"],
)
burnin_alerts_total = Counter(
    "burnin_alerts_total",
    "Total number of burn-in alert events raised (noise engine detections).",
    ["pattern_type"],
)
burnin_gaps_total = Counter(
    "burnin_gaps_total",
    "Total number of temporal gaps >30 min detected across replay sessions.",
)

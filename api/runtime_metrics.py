"""
runtime_metrics.py — Phase R R-10

Prometheus metrics for Phase R: Autonomous Runtime Governance & Production Hardening.

All metrics are optional — Phase R modules wrap their import in try/except.
Do NOT import from api/metrics.py or api/live_metrics.py here — each metric
file owns its own set of names to avoid "Duplicated timeseries in CollectorRegistry".

Metrics owned here (Phase R):
  startup_health_score          Gauge
  startup_integrity_score       Gauge
  runtime_health_score          Gauge
  runtime_governance_score      Gauge
  operational_resilience_score  Gauge
  production_readiness_score    Gauge
  watchdog_health_score         Gauge
  recovery_success_rate         Gauge
  operational_decay_score       Gauge
  incident_count_total          Counter [severity]
  critical_incidents_total      Counter
  replay_integrity_score        Gauge
  deployment_safety_score       Gauge
  scheduler_drift_ms            Gauge
  restart_events_total          Counter [reason]
  restoration_integrity_score   Gauge
  loop_integrity_score          Gauge
  runtime_anomaly_score         Gauge
  long_running_stability_score  Gauge
  incident_severity_score       Gauge
  incident_frequency_score      Gauge
  operational_risk_score        Gauge
  rollback_risk_score           Gauge
  migration_integrity_score     Gauge
  compatibility_score           Gauge
  recovery_integrity_score      Gauge
  recovery_duration_ms          Gauge
  readiness_confidence          Gauge
  operational_maturity_score    Gauge
  state_consistency_score       Gauge
  replay_recovery_score         Gauge
"""

from prometheus_client import Gauge, Counter

# ── Startup & Initialization ───────────────────────────────────────────────────

startup_health_score = Gauge(
    "startup_health_score",
    "Overall health score at system startup (0-100) — AutonomousStartupManager",
)

startup_integrity_score = Gauge(
    "startup_integrity_score",
    "Integrity validation score during startup sequence (0-100) — AutonomousStartupManager",
)

# ── Runtime Health & Governance ────────────────────────────────────────────────

runtime_health_score = Gauge(
    "runtime_health_score",
    "Current runtime health score across all subsystems (0-100) — RuntimeHealthMonitor",
)

runtime_governance_score = Gauge(
    "runtime_governance_score",
    "Unified runtime governance score aggregating all Phase R subsystems (0-100) — AutonomousRuntimeGovernance",
)

operational_resilience_score = Gauge(
    "operational_resilience_score",
    "Operational resilience score: watchdog + stability + recovery + restoration (0-100) — AutonomousRuntimeGovernance",
)

production_readiness_score = Gauge(
    "production_readiness_score",
    "Production readiness score combining governance, deployment safety, and readiness confidence (0-100) — AutonomousRuntimeGovernance",
)

# ── Watchdog & Stability ───────────────────────────────────────────────────────

watchdog_health_score = Gauge(
    "watchdog_health_score",
    "Watchdog subsystem health score (0-100) — RuntimeWatchdogEngine",
)

long_running_stability_score = Gauge(
    "long_running_stability_score",
    "Long-running operational stability score over extended time window (0-100) — OperationalStabilityEngine",
)

# ── Recovery ──────────────────────────────────────────────────────────────────

recovery_success_rate = Gauge(
    "recovery_success_rate",
    "Fraction of recovery actions that succeeded in the last recovery cycle (0-100) — OperationalRecoveryEngine",
)

recovery_integrity_score = Gauge(
    "recovery_integrity_score",
    "Integrity score of the last recovery cycle based on pre/post checks (0-100) — OperationalRecoveryEngine",
)

recovery_duration_ms = Gauge(
    "recovery_duration_ms",
    "Total wall-clock duration of the last recovery cycle in milliseconds — OperationalRecoveryEngine",
)

# ── Incidents ─────────────────────────────────────────────────────────────────

incident_count_total = Counter(
    "incident_count_total",
    "Total number of incidents recorded, by severity level — IncidentTracker",
    ["severity"],  # LOW | MEDIUM | HIGH | CRITICAL | EMERGENCY
)

critical_incidents_total = Counter(
    "critical_incidents_total",
    "Total number of CRITICAL and EMERGENCY incidents ever recorded — IncidentTracker",
)

incident_severity_score = Gauge(
    "incident_severity_score",
    "Weighted severity score of currently active incidents (0-100, higher = worse) — AutonomousRuntimeGovernance",
)

incident_frequency_score = Gauge(
    "incident_frequency_score",
    "Incident frequency score: incidents per hour over rolling window (0-100) — ProductionReadinessClassifier",
)

# ── Deployment & Compatibility ────────────────────────────────────────────────

deployment_safety_score = Gauge(
    "deployment_safety_score",
    "Deployment validation safety score (0-100) — DeploymentValidationEngine",
)

migration_integrity_score = Gauge(
    "migration_integrity_score",
    "Data migration integrity score for schema/state migrations (0-100) — MigrationValidator",
)

compatibility_score = Gauge(
    "compatibility_score",
    "Runtime compatibility score across dependencies and APIs (0-100) — CompatibilityChecker",
)

# ── Replay & State ─────────────────────────────────────────────────────────────

replay_integrity_score = Gauge(
    "replay_integrity_score",
    "Execution replay integrity/fidelity score (0-100) — LiveExecutionReplayEngine",
)

replay_recovery_score = Gauge(
    "replay_recovery_score",
    "Score measuring replay recovery quality after state restoration (0-100) — LiveExecutionReplayEngine",
)

state_consistency_score = Gauge(
    "state_consistency_score",
    "Consistency score between restored state and expected state (0-100) — OperationalStateRestorationEngine",
)

restoration_integrity_score = Gauge(
    "restoration_integrity_score",
    "State restoration integrity score after recovery or restart (0-100) — OperationalStateRestorationEngine",
)

# ── Loop & Scheduler ──────────────────────────────────────────────────────────

loop_integrity_score = Gauge(
    "loop_integrity_score",
    "Governance loop integrity score: timing, completeness, error rate (0-100) — GovernanceLoopMonitor",
)

scheduler_drift_ms = Gauge(
    "scheduler_drift_ms",
    "Scheduler timing drift in milliseconds relative to expected schedule — SchedulerDriftMonitor",
)

# ── Risk & Decay ──────────────────────────────────────────────────────────────

runtime_consistency_score = Gauge(
    "runtime_consistency_score",
    "Runtime consistency score across recent stability snapshots (0-100) — StabilityAnalyzer",
)

operational_decay_score = Gauge(
    "operational_decay_score",
    "Operational decay score measuring degradation over time (0-100, higher = more decay) — OperationalDecayMonitor",
)

runtime_anomaly_score = Gauge(
    "runtime_anomaly_score",
    "Runtime anomaly detection score (0-100, higher = more anomalous) — RuntimeAnomalyDetector",
)

operational_risk_score = Gauge(
    "operational_risk_score",
    "Overall operational risk score combining all risk signals (0-100) — AutonomousRuntimeGovernance",
)

rollback_risk_score = Gauge(
    "rollback_risk_score",
    "Current risk score for triggering an autonomous rollback (0-100) — OperationalRecoveryEngine",
)

# ── Readiness ─────────────────────────────────────────────────────────────────

readiness_confidence = Gauge(
    "readiness_confidence",
    "Confidence score for the current production readiness classification (0-100) — ProductionReadinessClassifier",
)

operational_maturity_score = Gauge(
    "operational_maturity_score",
    "Operational maturity score based on passed readiness dimensions (0-100) — ProductionReadinessClassifier",
)

# ── Restart Events ─────────────────────────────────────────────────────────────

restart_events_total = Counter(
    "restart_events_total",
    "Total number of subsystem restart events, by reason — OperationalRecoveryEngine",
    ["reason"],  # governance_loop | pipelines | state_restoration | watchdog | scheduled
)

"""Custom Prometheus metrics for data-core.

All counters and histograms are module-level singletons — import from here
to avoid re-registration errors when the module is imported multiple times.

Metric groups
─────────────
• price_feed_*          Legacy price-feed endpoint counters (ecommerce)
• pipeline_*            Per-stage timing and volume (collection / normalization / analytics)
• collection_*          Per-domain / per-source collection counters
• job_dead_letters_*    Scheduler dead-letter tracking
• circuit_breaker_*     Circuit-breaker state
• db_pool_*             PostgreSQL connection pool utilisation
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Summary

# ──────────────────────────────────────────────────────────────────────────────
# Legacy price-feed metrics (ecommerce)
# ──────────────────────────────────────────────────────────────────────────────

price_feed_requests_total = Counter(
    "price_feed_requests_total",
    "Total number of /price-feed requests",
    ["cursor_used"],  # 'yes' | 'no'
)

price_feed_items_served_total = Counter(
    "price_feed_items_served_total",
    "Total number of price-feed items returned to consumers",
    ["store_name"],
)

price_feed_response_size = Histogram(
    "price_feed_response_size_items",
    "Distribution of item counts returned per price-feed request",
    buckets=[0, 1, 10, 50, 100, 200, 500, 1000],
)

# ──────────────────────────────────────────────────────────────────────────────
# Pipeline stage metrics  (collection → normalization → analytics)
# ──────────────────────────────────────────────────────────────────────────────

# Labels: domain (crypto | ecommerce | real_estate | sports_betting | trading)
#         stage  (collection | normalization | analytics)
#         status (success | error)

pipeline_stage_runs_total = Counter(
    "pipeline_stage_runs_total",
    "Total number of pipeline stage executions",
    ["domain", "stage", "status"],
)

pipeline_stage_duration_seconds = Histogram(
    "pipeline_stage_duration_seconds",
    "Wall-clock duration of a single pipeline stage execution",
    ["domain", "stage"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300],
)

pipeline_items_processed_total = Counter(
    "pipeline_items_processed_total",
    "Total number of items processed per stage",
    ["domain", "stage"],
)

pipeline_items_error_total = Counter(
    "pipeline_items_error_total",
    "Total number of items that caused processing errors per stage",
    ["domain", "stage"],
)

# Active (in-flight) stage executions
pipeline_stage_active = Gauge(
    "pipeline_stage_active",
    "Number of currently executing pipeline stages",
    ["domain", "stage"],
)

# Last successful run timestamp (Unix epoch) – useful for staleness alerts
pipeline_stage_last_success_timestamp = Gauge(
    "pipeline_stage_last_success_timestamp_seconds",
    "Unix timestamp of the last successful pipeline stage completion",
    ["domain", "stage"],
)

# ──────────────────────────────────────────────────────────────────────────────
# Collection-specific metrics
# ──────────────────────────────────────────────────────────────────────────────

collection_raw_saved_total = Counter(
    "collection_raw_saved_total",
    "Total number of raw records saved per collector",
    ["domain", "collector_name"],
)

collection_raw_duplicates_total = Counter(
    "collection_raw_duplicates_total",
    "Total number of duplicate raw records skipped",
    ["domain", "collector_name"],
)

collection_errors_total = Counter(
    "collection_errors_total",
    "Total number of collection errors per collector",
    ["domain", "collector_name", "error_type"],
)

collection_duration_seconds = Histogram(
    "collection_duration_seconds",
    "Wall-clock duration of a collector run",
    ["domain", "collector_name"],
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600],
)

collection_attempts_total = Counter(
    "collection_attempts_total",
    "Total number of individual target collection attempts",
    ["domain", "collector_name"],
)

collection_success_total = Counter(
    "collection_success_total",
    "Total number of successfully scraped targets (price extracted)",
    ["domain", "collector_name"],
)

collection_failed_total = Counter(
    "collection_failed_total",
    "Total number of failed target collection attempts",
    ["domain", "collector_name"],
)

collection_empty_total = Counter(
    "collection_empty_total",
    "Total number of targets that returned empty/no-price payload",
    ["domain", "collector_name"],
)

# Last timestamp (Unix epoch) of a successful / failed batch run per collector
collector_last_success_timestamp = Gauge(
    "collector_last_success_timestamp_seconds",
    "Unix timestamp of the last successful collector batch run",
    ["domain", "collector_name"],
)

collector_last_failure_timestamp = Gauge(
    "collector_last_failure_timestamp_seconds",
    "Unix timestamp of the last failed collector batch run",
    ["domain", "collector_name"],
)


def _active_ecommerce_targets() -> int:
    from database.models import CollectionTarget
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return (
            db.query(CollectionTarget)
            .filter(
                CollectionTarget.module == "ecommerce",
                CollectionTarget.active.is_(True),
            )
            .count()
        )
    except Exception:
        return 0
    finally:
        db.close()


collector_active_targets = Gauge(
    "collector_active_targets",
    "Current number of active collection targets",
    ["module"],
)
# Registered as a per-module gauge; ecommerce is the only live module today.
# Additional modules can call .labels(module=...).set(...) from their schedulers.
collector_active_targets.labels(module="ecommerce").set_function(_active_ecommerce_targets)

# ──────────────────────────────────────────────────────────────────────────────
# Trading analytics metrics  (crypto OHLCV pipeline)
# ──────────────────────────────────────────────────────────────────────────────

# Labels: symbol (BTC/USDT etc.)  timeframe (15m | 1h)  signal (BUY | SELL | HOLD | ...)
trading_signal_total = Counter(
    "trading_signal_total",
    "Total number of trading signals generated by TradingAnalyticsProcessor",
    ["symbol", "timeframe", "signal"],
)

# Labels: symbol  timeframe  regime (TRENDING_UP | TRENDING_DOWN | RANGING | UNKNOWN)
trading_regime_total = Counter(
    "trading_regime_total",
    "Total number of market regime classifications computed",
    ["symbol", "timeframe", "regime"],
)

# Labels: symbol  timeframe
trading_confidence_histogram = Histogram(
    "trading_confidence_score",
    "Distribution of confidence scores produced by TradingAnalyticsProcessor",
    ["symbol", "timeframe"],
    buckets=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
)

# ──────────────────────────────────────────────────────────────────────────────
# Dead-letter + scheduler
# ──────────────────────────────────────────────────────────────────────────────

job_dead_letters_total = Counter(
    "job_dead_letters_total",
    "Total number of scheduler jobs that exhausted retries and wrote a dead letter",
    ["job_name"],
)


def _unresolved_job_dead_letter_count() -> int:
    from database.models import CollectorError
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return (
            db.query(CollectorError)
            .filter(
                CollectorError.error_type == "JobDeadLetter",
                CollectorError.resolved_at.is_(None),
            )
            .count()
        )
    except Exception:
        return 0
    finally:
        db.close()


job_dead_letters_unresolved = Gauge(
    "job_dead_letters_unresolved",
    "Current number of unresolved scheduler JobDeadLetter records",
)
job_dead_letters_unresolved.set_function(_unresolved_job_dead_letter_count)

# ──────────────────────────────────────────────────────────────────────────────
# Circuit breaker
# ──────────────────────────────────────────────────────────────────────────────

circuit_breaker_opens_total = Counter(
    "circuit_breaker_opens_total",
    "Total number of times a source circuit was opened",
    ["module", "source_name"],
)


def _open_circuit_count() -> int:
    from database.models import CollectorError
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return (
            db.query(CollectorError)
            .filter(
                CollectorError.error_type == "CircuitOpen",
                CollectorError.resolved_at.is_(None),
            )
            .count()
        )
    except Exception:
        return 0
    finally:
        db.close()


circuit_breaker_open_sources = Gauge(
    "circuit_breaker_open_sources",
    "Current number of sources with an open circuit breaker",
)
circuit_breaker_open_sources.set_function(_open_circuit_count)

# ──────────────────────────────────────────────────────────────────────────────
# Database pool
# ──────────────────────────────────────────────────────────────────────────────


def _db_pool_size() -> int:
    try:
        from database.session import engine
        pool = engine.pool
        return pool.size()  # type: ignore[attr-defined]
    except Exception:
        return 0


def _db_pool_checked_out() -> int:
    try:
        from database.session import engine
        pool = engine.pool
        return pool.checkedout()  # type: ignore[attr-defined]
    except Exception:
        return 0


db_pool_size = Gauge("db_pool_size", "SQLAlchemy connection pool size")
db_pool_size.set_function(_db_pool_size)

db_pool_checked_out = Gauge(
    "db_pool_checked_out",
    "Number of connections currently checked out from the pool",
)
db_pool_checked_out.set_function(_db_pool_checked_out)


# ──────────────────────────────────────────────────────────────────────────────
# Backtesting / replay metrics  (data-core research layer)
# ──────────────────────────────────────────────────────────────────────────────

backtest_runs_total = Counter(
    "backtest_runs_total",
    "Total number of backtest/replay runs executed",
    ["symbol", "timeframe", "mode"],  # mode: realistic | simple | db_replay | walk_forward
)

backtest_duration_seconds = Histogram(
    "backtest_duration_seconds",
    "Wall-clock duration of a backtest run",
    ["symbol", "timeframe", "mode"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300],
)

backtest_candles_processed_total = Counter(
    "backtest_candles_processed_total",
    "Total number of OHLCV candles processed across all backtests",
    ["symbol", "timeframe"],
)

ohlcv_integrity_checks_total = Counter(
    "ohlcv_integrity_checks_total",
    "Total number of OHLCV integrity checks run",
    ["symbol", "timeframe", "status"],  # status: CLEAN | ACCEPTABLE | DEGRADED | CRITICAL
)

ohlcv_gaps_detected_total = Counter(
    "ohlcv_gaps_detected_total",
    "Total number of temporal gaps detected in OHLCV data",
    ["symbol", "timeframe"],
)

# ── Phase K FASE 12: Quantitative Observability — Crypto Research ─────────────

sweep_runs_total = Counter(
    "sweep_runs_total",
    "Total number of parameter sweep runs executed",
    ["strategy_id", "symbol", "timeframe"],
)

sweep_combinations_tested_total = Counter(
    "sweep_combinations_tested_total",
    "Total number of parameter combinations evaluated across all sweeps",
    ["strategy_id"],
)

experiment_records_total = Counter(
    "experiment_records_total",
    "Total number of experiment records persisted to JSONL storage",
    ["strategy_id", "replay_dataset"],
)

strategy_composite_score = Gauge(
    "strategy_composite_score",
    "Current best composite score (0-100) for a strategy (latest from strategy_ranker)",
    ["strategy_id"],
)

scenario_runs_total = Counter(
    "scenario_runs_total",
    "Total number of named scenario replay runs",
    ["scenario", "strategy_id", "symbol"],
)

portfolio_simulations_total = Counter(
    "portfolio_simulations_total",
    "Total number of multi-strategy portfolio simulations run",
    ["n_strategies"],
)

dataset_qa_fleet_score = Gauge(
    "dataset_qa_fleet_score",
    "Average integrity score across all OHLCV symbol/timeframe pairs in the fleet",
)

dataset_qa_critical_count = Gauge(
    "dataset_qa_critical_count",
    "Number of OHLCV pairs with quality_class=CRITICAL",
)

# ── Phase L FASE 14: Expanded Quantitative Observability ─────────────────────

orchestration_runs_total = Counter(
    "orchestration_runs_total",
    "Total number of ResearchOrchestrator pipeline runs",
    ["success"],  # 'true' | 'false'
)

strategy_degradation_total = Counter(
    "strategy_degradation_total",
    "Total number of strategy degradation signals detected",
    ["strategy_id", "severity"],  # severity: low | medium | high
)

portfolio_rebalance_total = Counter(
    "portfolio_rebalance_total",
    "Total number of portfolio rebalance simulations executed",
    ["rebalance_type"],  # equal_weight | vol_target | exposure_balance
)

dataset_drift_score = Gauge(
    "dataset_drift_score",
    "Current drift magnitude for an OHLCV pair (0.0 = no drift, 1.0 = max drift)",
    ["symbol", "timeframe"],
)

replay_stress_total = Counter(
    "replay_stress_total",
    "Total number of scenario stress replay runs",
    ["scenario", "strategy_id"],
)

scenario_stress_score = Gauge(
    "scenario_stress_score",
    "Current stress score (0-100) for a strategy in a named scenario",
    ["scenario", "strategy_id"],
)

strategy_consistency_score = Gauge(
    "strategy_consistency_score",
    "Strategy consistency score (0-100) from StrategyIntelligenceAnalyzer",
    ["strategy_id"],
)

portfolio_correlation_avg = Gauge(
    "portfolio_correlation_avg",
    "Average pairwise correlation between strategies in the portfolio",
)

# ──────────────────────────────────────────────────────────────────────────────
# Phase M FASE 17 — Adaptive Quant Intelligence Metrics
# ──────────────────────────────────────────────────────────────────────────────

strategy_degradation_score = Gauge(
    "strategy_degradation_score",
    "Quantitative degradation score (0-100) from StrategyDegradationIntelligence",
    ["strategy_id"],
)

strategy_health_score = Gauge(
    "strategy_health_score",
    "Strategy health score (0-100, higher = healthier) from DegradationIntelligence",
    ["strategy_id"],
)

strategy_fragility_score = Gauge(
    "strategy_fragility_score",
    "Parameter fragility score (0-100) from FragilityIntelligence",
    ["strategy_id"],
)

strategy_overfitting_score = Gauge(
    "strategy_overfitting_score",
    "Overfitting risk score (0-100) from FragilityIntelligence",
    ["strategy_id"],
)

portfolio_health_score = Gauge(
    "portfolio_health_score",
    "Adaptive portfolio health score (0-100) from AdaptivePortfolioIntelligence",
)

research_loop_runs_total = Counter(
    "research_loop_runs_total",
    "Total adaptive research loop iterations executed",
    ["status"],   # success | error
)

# ──────────────────────────────────────────────────────────────────────────────
# Phase N FASE 11 — Autonomous Adaptive Quant Evolution Metrics
# ──────────────────────────────────────────────────────────────────────────────

market_drift_score = Gauge(
    "market_drift_score",
    "Composite market drift score (0-100) from MarketDriftIntelligence",
)

edge_decay_score = Gauge(
    "edge_decay_score",
    "Fleet-wide edge decay score (0-100) — average degradation_score across strategies",
)

strategy_retirement_total = Counter(
    "strategy_retirement_total",
    "Total number of strategy retirement transitions recorded by StrategyLifecycleEngine",
    ["strategy_id"],
)

strategy_promotions_total = Counter(
    "strategy_promotions_total",
    "Total number of strategy promotion transitions recorded by StrategyLifecycleEngine",
    ["strategy_id"],
)

adaptive_exposure_score = Gauge(
    "adaptive_exposure_score",
    "Fleet average adaptive exposure score (0-100) from AdaptiveExposureIntelligence",
)

research_priority_score = Gauge(
    "research_priority_score",
    "Fleet research urgency score (0-100) from ResearchPrioritizer",
)

parameter_stability_score = Gauge(
    "parameter_stability_score",
    "Average parameter stability score (0-100) from ParameterIntelligence",
)

portfolio_resilience_score = Gauge(
    "portfolio_resilience_score",
    "Portfolio resilience score (0-100) from AdaptivePortfolioEvolution",
)

autonomous_recommendations_total = Counter(
    "autonomous_recommendations_total",
    "Total recommendations generated by the autonomous quant recommendation engine",
    ["type"],  # quant | lifecycle | drift | meta
)

# ──────────────────────────────────────────────────────────────────────────────
# Phase O FASE 11 — Autonomous Quant Governance Metrics
# ──────────────────────────────────────────────────────────────────────────────

market_survival_score = Gauge(
    "market_survival_score",
    "Market survival score (0-100) from MarketSurvivalIntelligence",
)

systemic_risk_score = Gauge(
    "systemic_risk_score",
    "Systemic risk score (0-100) — cascading degradation and contagion",
)

strategy_trust_score = Gauge(
    "strategy_trust_score",
    "Trust-gated activation score (0-100) from StrategyActivationEngine",
    ["strategy_id"],
)

portfolio_survival_score = Gauge(
    "portfolio_survival_score",
    "Portfolio survival score (0-100) from AutonomousPortfolioGovernor",
)

adaptive_risk_score = Gauge(
    "adaptive_risk_score",
    "Adaptive risk score (0-100) — contagion + hidden fragility + tail risk",
)

self_healing_score = Gauge(
    "self_healing_score",
    "Self-healing quality score (0-100) from SelfHealingIntelligence",
)

autonomous_execution_total = Counter(
    "autonomous_execution_total",
    "Total number of autonomous execution cycles completed",
    ["type"],  # execution_cycle | governance_cycle
)

autonomous_strategy_switch_total = Counter(
    "autonomous_strategy_switch_total",
    "Total number of autonomous strategy state transitions",
    ["strategy_id", "from_state", "to_state"],
)

adaptive_efficiency_score = Gauge(
    "adaptive_efficiency_score",
    "Adaptive optimization efficiency score (0-100) from MetaOptimizationIntelligence",
)

# ──────────────────────────────────────────────────────────────────────────────
# Phase P FASE 11 — Autonomous Validation & Micro-Live Readiness Metrics
# ──────────────────────────────────────────────────────────────────────────────

autonomy_stability_score = Gauge(
    "autonomy_stability_score",
    "Stability of autonomous behavior (0-100) from AutonomousStabilityIntelligence",
)

capital_survival_score = Gauge(
    "capital_survival_score",
    "Capital preservation validation score (0-100) from CapitalPreservationValidator",
)

live_readiness_score = Gauge(
    "live_readiness_score",
    "Micro-live readiness gate score (0-100) from MicroLiveReadinessEngine",
)

governance_drift_score = Gauge(
    "governance_drift_score",
    "Governance quality drift (0-100, 0=no drift) from GovernanceDriftIntelligence",
)

execution_realism_score = Gauge(
    "execution_realism_score",
    "Execution simulation realism score (0-100) from ExecutionSimulationEngine",
)

preservation_efficiency_score = Gauge(
    "preservation_efficiency_score",
    "Capital preservation efficiency (0-100) from CapitalPreservationValidator",
)

autonomous_validation_cycles_total = Counter(
    "autonomous_validation_cycles_total",
    "Total number of autonomous validation loop cycles completed",
    ["status"],  # ok | partial | error
)

catastrophic_scenarios_total = Counter(
    "catastrophic_scenarios_total",
    "Total number of catastrophic scenarios simulated",
    ["scenario"],  # flash_crash | cascading_volatility | etc.
)

emergency_contractions_total = Counter(
    "emergency_contractions_total",
    "Total number of emergency exposure contractions triggered by safe constraints",
    ["type"],  # emergency | constraint
)

# ──────────────────────────────────────────────────────────────────────────────
# Phase RELIABILITY — Scraper reliability, drift, quality, anti-bot metrics
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Phase WATCHDOG — Operational watchdog metrics
# ──────────────────────────────────────────────────────────────────────────────

operational_watchdog_status = Gauge(
    "operational_watchdog_status",
    "Current watchdog check status per check (0=ok, 1=warning, 2=critical)",
    ["check"],  # collection | normalization | scraper_quality | telegram
)

last_raw_collection_age_seconds = Gauge(
    "last_raw_collection_age_seconds",
    "Seconds since the most recent raw_collection record was inserted (any source)",
)

last_normalized_offer_age_seconds = Gauge(
    "last_normalized_offer_age_seconds",
    "Seconds since the most recent normalized_product record was written",
)

last_telegram_post_age_seconds = Gauge(
    "last_telegram_post_age_seconds",
    "Seconds since the last successful Telegram publication (from poupi-baby callback)",
)

raw_to_normalized_success_rate = Gauge(
    "raw_to_normalized_success_rate",
    "Fraction of raw records successfully normalized in the last 24h (0.0 – 1.0)",
)

telegram_publish_success_total = Counter(
    "telegram_publish_success_total",
    "Total Telegram alert messages successfully sent by the watchdog notifier",
)

telegram_publish_failure_total = Counter(
    "telegram_publish_failure_total",
    "Total Telegram alert messages that failed to send from the watchdog notifier",
)

domains_with_active_alerts = Gauge(
    "domains_with_active_alerts",
    "Number of scraper domains currently with at least one active watchdog alert",
)

watchdog_checks_total = Counter(
    "watchdog_checks_total",
    "Total number of watchdog check runs completed",
    ["status"],  # ok | warning | critical
)

# ──────────────────────────────────────────────────────────────────────────────
# Phase RELIABILITY — Scraper reliability, drift, quality, anti-bot metrics
# ──────────────────────────────────────────────────────────────────────────────

scraper_quality_score = Histogram(
    "scraper_quality_score",
    "Payload quality score (0-100) per scraper domain and strategy",
    ["source_name", "strategy"],
    buckets=[10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
)

scraper_fallback_depth_total = Counter(
    "scraper_fallback_depth_total",
    "Total number of scrape attempts that used each strategy (tracks primary vs fallback usage)",
    ["source_name", "strategy"],  # strategy: vtex_api | json_ld | meta_css | ng_state | unknown
)

scraper_anti_bot_detections_total = Counter(
    "scraper_anti_bot_detections_total",
    "Total number of anti-bot patterns detected per source and detection type",
    ["source_name", "detection_type"],  # captcha | cloudflare | rate_limit | access_denied | honeypot | redirect_loop
)

scraper_drift_events_total = Counter(
    "scraper_drift_events_total",
    "Total number of structural drift events detected per source",
    ["source_name", "drift_type", "risk_level"],
)

scraper_drift_risk = Gauge(
    "scraper_drift_risk",
    "Current highest drift risk level for a scraper source (0=none 1=low 2=medium 3=high 4=critical)",
    ["source_name"],
)

# ──────────────────────────────────────────────────────────────────────────────
# Helper: context manager for pipeline stage instrumentation
# ──────────────────────────────────────────────────────────────────────────────

import time
from contextlib import contextmanager
from typing import Generator


@contextmanager
def measure_pipeline_stage(domain: str, stage: str) -> Generator[None, None, None]:
    """Context manager that records duration, status and item-active gauge.

    Usage::

        with measure_pipeline_stage("crypto", "analytics"):
            processor.run(limit=100)
    """
    pipeline_stage_active.labels(domain=domain, stage=stage).inc()
    start = time.perf_counter()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        pipeline_stage_runs_total.labels(domain=domain, stage=stage, status="error").inc()
        raise
    else:
        pipeline_stage_runs_total.labels(domain=domain, stage=stage, status="success").inc()
        pipeline_stage_last_success_timestamp.labels(domain=domain, stage=stage).set(time.time())
    finally:
        elapsed = time.perf_counter() - start
        pipeline_stage_duration_seconds.labels(domain=domain, stage=stage).observe(elapsed)
        pipeline_stage_active.labels(domain=domain, stage=stage).dec()

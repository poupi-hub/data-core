"""
live_metrics_updater.py

Lê os arquivos JSONL dos módulos Phase O/P/Q/R e atualiza os Gauges Prometheus
no processo do API. Chamado pelo scheduler ou pelo middleware de /metrics.

Sem este módulo, os Gauges ficam em 0 para sempre porque os scripts CLI
rodam em processos separados e não compartilham o registry Prometheus com o API.

Uso:
  from api.live_metrics_updater import refresh_live_metrics
  refresh_live_metrics()  # chame periodicamente via scheduler

Ou monte em um endpoint:
  @router.post("/internal/metrics/refresh", include_in_schema=False)
  def refresh(): refresh_live_metrics(); return {"ok": True}
"""

from __future__ import annotations

import json
from pathlib import Path

# ── Paths (relativos ao cwd do API, normalmente raiz do projeto) ────────────────

_DATA = Path("data")

_SOURCES: dict[str, tuple[Path, str]] = {
    # (arquivo_jsonl, campo_no_jsonl)
    "live_governance_score":       (_DATA / "live_governance_summary.jsonl",         "live_governance_score"),
    "execution_quality_score":     (_DATA / "live_execution_audit_summary.jsonl",     "execution_quality_score"),
    "live_slippage_bps":           (_DATA / "live_execution_audit_summary.jsonl",     "avg_slippage_bps"),
    "execution_latency_ms":        (_DATA / "live_execution_audit_summary.jsonl",     "avg_latency_ms"),
    "divergence_score":            (_DATA / "paper_vs_live_divergence_log.jsonl",     "divergence_score"),
    "live_consistency_score":      (_DATA / "paper_vs_live_divergence_log.jsonl",     "live_consistency_score"),
    "guardian_emergency_level":    (_DATA / "live_guardian_log.jsonl",                "emergency_level"),
    "contraction_multiplier":      (_DATA / "live_guardian_log.jsonl",                "contraction_multiplier"),
    "exchange_instability_score":  (_DATA / "live_guardian_log.jsonl",                "exchange_instability"),
    "live_drawdown_pct":           (_DATA / "live_capital_preservation_log.jsonl",    "daily_drawdown_pct"),
    "live_capital_exposure_pct":   (_DATA / "live_capital_preservation_log.jsonl",    "current_exposure_pct"),
    "live_readiness_score":        (_DATA / "live_readiness_revalidation_log.jsonl",  "continuous_live_readiness_score"),
    # autonomous_freeze_state: 1 se guardian_state em {FROZEN, ROLLBACK}, 0 caso contrário
    "_guardian_state":             (_DATA / "live_guardian_log.jsonl",                "guardian_state"),
}

# Phase O/P metrics (governance_history.jsonl, validation_loop_history.jsonl, etc.)
_SOURCES_PHASE_OP: dict[str, tuple[Path, str]] = {
    "governance_health_score":     (_DATA / "governance_history.jsonl",               "governance_health_score"),
    "autonomy_stability_score":    (_DATA / "stability_intelligence_log.jsonl",        "autonomy_stability_score"),
    "capital_survival_score":      (_DATA / "capital_preservation_log.jsonl",          "capital_survival_score"),
    "live_readiness_score_p":      (_DATA / "live_readiness_log.jsonl",                "live_readiness_score"),
    "governance_drift_score":      (_DATA / "governance_drift_log.jsonl",              "governance_drift_score"),
    "execution_realism_score":     (_DATA / "execution_simulation_log.jsonl",          "execution_realism_score"),
    "market_survival_score":       (_DATA / "behavior_audit_log.jsonl",                "system_autonomy_score"),
    "systemic_risk_score":         (_DATA / "catastrophic_simulation_log.jsonl",       "catastrophic_survival_score"),
}


def _read_last_value(path: Path, field: str, default: float = 0.0) -> float:
    """Lê o último valor de um campo em um arquivo JSONL."""
    if not path.exists():
        return default
    last: float = default
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                        val = rec.get(field, default)
                        if val is not None:
                            last = float(val)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
    except Exception:
        pass
    return last


def refresh_live_metrics() -> dict[str, float]:
    """
    Lê os JSONLs e atualiza todos os Gauges Phase Q/R no processo do API.
    Retorna dict com os valores atualizados (para debug/logging).
    Inclui chamada a refresh_runtime_metrics() para métricas Phase R.
    """
    try:
        import api.live_metrics as lm
    except ImportError:
        return {}

    updated: dict[str, float] = {}

    # ── Phase Q metrics ────────────────────────────────────────────────────────

    def _set(gauge, key: str, path: Path, field: str, default: float = 0.0) -> None:
        val = _read_last_value(path, field, default)
        try:
            gauge.set(val)
            updated[key] = val
        except Exception:
            pass

    _set(lm.live_governance_score,       "live_governance_score",      _DATA / "live_governance_summary.jsonl",        "live_governance_score")
    _set(lm.execution_quality_score,     "execution_quality_score",    _DATA / "live_execution_audit_summary.jsonl",    "execution_quality_score")
    _set(lm.live_slippage_bps,           "live_slippage_bps",          _DATA / "live_execution_audit_summary.jsonl",    "avg_slippage_bps")
    _set(lm.execution_latency_ms,        "execution_latency_ms",       _DATA / "live_execution_audit_summary.jsonl",    "avg_latency_ms")
    _set(lm.divergence_score,            "divergence_score",           _DATA / "paper_vs_live_divergence_log.jsonl",    "divergence_score")
    _set(lm.live_consistency_score,      "live_consistency_score",     _DATA / "paper_vs_live_divergence_log.jsonl",    "live_consistency_score")
    _set(lm.guardian_emergency_level,    "guardian_emergency_level",   _DATA / "live_guardian_log.jsonl",               "emergency_level")
    _set(lm.contraction_multiplier,      "contraction_multiplier",     _DATA / "live_guardian_log.jsonl",               "contraction_multiplier", 1.0)
    _set(lm.exchange_instability_score,  "exchange_instability_score", _DATA / "live_guardian_log.jsonl",               "exchange_instability")
    _set(lm.live_drawdown_pct,           "live_drawdown_pct",          _DATA / "live_capital_preservation_log.jsonl",   "daily_drawdown_pct")
    _set(lm.live_capital_exposure_pct,   "live_capital_exposure_pct",  _DATA / "live_capital_preservation_log.jsonl",   "current_exposure_pct")
    _set(lm.continuous_live_readiness_score, "continuous_live_readiness_score", _DATA / "live_readiness_revalidation_log.jsonl", "continuous_live_readiness_score")

    # autonomous_freeze_state: derivado do guardian_state
    guardian_state = ""
    guardian_path  = _DATA / "live_guardian_log.jsonl"
    if guardian_path.exists():
        guardian_state = ""
        try:
            with open(guardian_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            guardian_state = json.loads(line).get("guardian_state", "")
                        except Exception:
                            pass
        except Exception:
            pass
    freeze_val = 1.0 if guardian_state in ("FROZEN", "ROLLBACK") else 0.0
    try:
        lm.autonomous_freeze_state.set(freeze_val)
        updated["autonomous_freeze_state"] = freeze_val
    except Exception:
        pass

    # ── Phase O/P metrics (best-effort) ───────────────────────────────────────
    try:
        import api.metrics as m

        # market_survival_score → system_autonomy_score do behavior audit
        val = _read_last_value(_DATA / "behavior_audit_log.jsonl", "system_autonomy_score", 75.0)
        try:
            m.market_survival_score.set(val)
            updated["market_survival_score"] = val
        except Exception:
            pass

        val = _read_last_value(_DATA / "governance_history.jsonl", "governance_health_score", 75.0)
        try:
            # governance_health_score nao existe em api/metrics mas pode ser adicionado
            pass
        except Exception:
            pass

        val = _read_last_value(_DATA / "capital_preservation_log.jsonl", "capital_survival_score", 75.0)
        try:
            m.capital_survival_score.set(val)
            updated["capital_survival_score"] = val
        except Exception:
            pass

        val = _read_last_value(_DATA / "live_readiness_log.jsonl", "live_readiness_score", 75.0)
        try:
            m.live_readiness_score.set(val)
            updated["live_readiness_score_p"] = val
        except Exception:
            pass

        val = _read_last_value(_DATA / "governance_drift_log.jsonl", "governance_drift_score", 0.0)
        try:
            m.governance_drift_score.set(val)
            updated["governance_drift_score"] = val
        except Exception:
            pass

        val = _read_last_value(_DATA / "execution_simulation_log.jsonl", "execution_realism_score", 75.0)
        try:
            m.execution_realism_score.set(val)
            updated["execution_realism_score"] = val
        except Exception:
            pass

        val = _read_last_value(_DATA / "stability_intelligence_log.jsonl", "autonomy_stability_score", 75.0)
        try:
            m.autonomy_stability_score.set(val)
            updated["autonomy_stability_score"] = val
        except Exception:
            pass

    except Exception:
        pass

    # ── Phase R metrics ────────────────────────────────────────────────────────
    updated.update(refresh_runtime_metrics())

    # ── Phase S metrics ────────────────────────────────────────────────────────
    updated.update(refresh_burnin_metrics())

    return updated


def refresh_runtime_metrics() -> dict[str, float]:
    """
    Lê os JSONLs de Phase R e atualiza todos os Gauges de runtime_metrics
    no processo do API.
    Retorna dict com os valores atualizados (para debug/logging).
    """
    try:
        import api.runtime_metrics as rm
    except ImportError:
        return {}

    updated: dict[str, float] = {}

    def _set_rm(gauge, key: str, path: Path, field: str, default: float = 0.0) -> None:
        val = _read_last_value(path, field, default)
        try:
            gauge.set(val)
            updated[key] = val
        except Exception:
            pass

    # ── startup_log.jsonl ──────────────────────────────────────────────────────
    _set_rm(rm.startup_health_score,       "startup_health_score",       _DATA / "startup_log.jsonl",               "startup_health_score")
    _set_rm(rm.startup_integrity_score,    "startup_integrity_score",    _DATA / "startup_log.jsonl",               "startup_integrity_score")

    # ── state_restoration_log.jsonl ────────────────────────────────────────────
    _set_rm(rm.restoration_integrity_score, "restoration_integrity_score", _DATA / "state_restoration_log.jsonl",   "restoration_integrity_score")
    _set_rm(rm.state_consistency_score,    "state_consistency_score",    _DATA / "state_restoration_log.jsonl",     "state_consistency_score")
    _set_rm(rm.replay_recovery_score,      "replay_recovery_score",      _DATA / "state_restoration_log.jsonl",     "replay_recovery_score")

    # ── watchdog_log.jsonl ─────────────────────────────────────────────────────
    _set_rm(rm.watchdog_health_score,      "watchdog_health_score",      _DATA / "watchdog_log.jsonl",              "watchdog_health_score")
    _set_rm(rm.loop_integrity_score,       "loop_integrity_score",       _DATA / "watchdog_log.jsonl",              "loop_integrity_score")
    _set_rm(rm.runtime_anomaly_score,      "runtime_anomaly_score",      _DATA / "watchdog_log.jsonl",              "runtime_anomaly_score")

    # ── stability_log.jsonl ────────────────────────────────────────────────────
    _set_rm(rm.runtime_health_score,            "runtime_health_score",            _DATA / "stability_log.jsonl",   "runtime_health_score")
    _set_rm(rm.operational_decay_score,         "operational_decay_score",         _DATA / "stability_log.jsonl",   "operational_decay_score")
    _set_rm(rm.long_running_stability_score,    "long_running_stability_score",    _DATA / "stability_log.jsonl",   "long_running_stability_score")
    _set_rm(rm.runtime_consistency_score,       "runtime_consistency_score",       _DATA / "stability_log.jsonl",   "runtime_consistency_score")

    # ── deployment_validation_log.jsonl ────────────────────────────────────────
    _set_rm(rm.deployment_safety_score,    "deployment_safety_score",    _DATA / "deployment_validation_log.jsonl", "deployment_safety_score")
    _set_rm(rm.migration_integrity_score,  "migration_integrity_score",  _DATA / "deployment_validation_log.jsonl", "migration_integrity_score")
    _set_rm(rm.rollback_risk_score,        "rollback_risk_score",        _DATA / "deployment_validation_log.jsonl", "rollback_risk_score")
    _set_rm(rm.compatibility_score,        "compatibility_score",        _DATA / "deployment_validation_log.jsonl", "compatibility_score")

    # ── incident_log.jsonl (última entrada) ────────────────────────────────────
    _set_rm(rm.incident_severity_score,    "incident_severity_score",    _DATA / "incident_log.jsonl",              "incident_severity_score")
    _set_rm(rm.incident_frequency_score,   "incident_frequency_score",   _DATA / "incident_log.jsonl",              "incident_frequency_score")
    _set_rm(rm.operational_risk_score,     "operational_risk_score",     _DATA / "incident_log.jsonl",              "operational_risk_score")

    # ── recovery_log.jsonl ─────────────────────────────────────────────────────
    _set_rm(rm.recovery_success_rate,      "recovery_success_rate",      _DATA / "recovery_log.jsonl",              "recovery_success_rate")
    _set_rm(rm.recovery_integrity_score,   "recovery_integrity_score",   _DATA / "recovery_log.jsonl",              "recovery_integrity_score")
    _set_rm(rm.recovery_duration_ms,       "recovery_duration_ms",       _DATA / "recovery_log.jsonl",              "recovery_duration_ms")

    # ── runtime_governance_log.jsonl ───────────────────────────────────────────
    _set_rm(rm.runtime_governance_score,       "runtime_governance_score",       _DATA / "runtime_governance_log.jsonl",   "runtime_governance_score")
    _set_rm(rm.operational_resilience_score,   "operational_resilience_score",   _DATA / "runtime_governance_log.jsonl",   "operational_resilience_score")
    _set_rm(rm.production_readiness_score,     "production_readiness_score",     _DATA / "runtime_governance_log.jsonl",   "production_readiness_score")

    # ── production_readiness_log.jsonl ─────────────────────────────────────────
    _set_rm(rm.readiness_confidence,       "readiness_confidence",       _DATA / "production_readiness_log.jsonl",  "readiness_confidence")
    _set_rm(rm.operational_maturity_score, "operational_maturity_score", _DATA / "production_readiness_log.jsonl",  "operational_maturity_score")

    return updated


def refresh_burnin_metrics() -> dict[str, float]:
    """
    Lê os JSONLs de Phase S e atualiza todos os Gauges de burnin_metrics
    no processo do API.
    Retorna dict com os valores atualizados (para debug/logging).
    """
    try:
        import api.burnin_metrics as bm
    except ImportError:
        return {}

    updated: dict[str, float] = {}

    def _set_bm(gauge, key: str, path: Path, field: str, default: float = 0.0) -> None:
        val = _read_last_value(path, field, default)
        try:
            gauge.set(val)
            updated[key] = val
        except Exception:
            pass

    # ── S-1: runtime_burnin_log.jsonl ─────────────────────────────────────────
    _set_bm(bm.burnin_stability_score,        "burnin_stability_score",        _DATA / "runtime_burnin_log.jsonl",        "burnin_stability_score")
    _set_bm(bm.runtime_burnin_score,          "runtime_burnin_score",          _DATA / "runtime_burnin_log.jsonl",        "runtime_burnin_score")
    _set_bm(bm.long_session_integrity_score,  "long_session_integrity_score",  _DATA / "runtime_burnin_log.jsonl",        "long_session_integrity_score")

    # ── S-2: metrics_integrity_log.jsonl ──────────────────────────────────────
    _set_bm(bm.metrics_integrity_score,       "metrics_integrity_score",       _DATA / "metrics_integrity_log.jsonl",     "metrics_integrity_score")
    _set_bm(bm.metrics_continuity_score,      "metrics_continuity_score",      _DATA / "metrics_integrity_log.jsonl",     "metrics_continuity_score")
    _set_bm(bm.observability_health_score,    "observability_health_score",    _DATA / "metrics_integrity_log.jsonl",     "observability_health_score")

    # ── S-3: dashboard_validation_log.jsonl ───────────────────────────────────
    _set_bm(bm.dashboard_integrity_score,          "dashboard_integrity_score",          _DATA / "dashboard_validation_log.jsonl",  "dashboard_integrity_score")
    _set_bm(bm.panel_health_score,                 "panel_health_score",                 _DATA / "dashboard_validation_log.jsonl",  "panel_health_score")
    _set_bm(bm.visualization_consistency_score,    "visualization_consistency_score",    _DATA / "dashboard_validation_log.jsonl",  "visualization_consistency_score")

    # ── S-4: collector_reliability_log.jsonl ──────────────────────────────────
    _set_bm(bm.collector_reliability_score,        "collector_reliability_score",        _DATA / "collector_reliability_log.jsonl", "collector_reliability_score")
    _set_bm(bm.normalization_integrity_score,      "normalization_integrity_score",      _DATA / "collector_reliability_log.jsonl", "normalization_integrity_score")
    _set_bm(bm.data_freshness_score,               "data_freshness_score",               _DATA / "collector_reliability_log.jsonl", "data_freshness_score")

    # ── S-5: replay_burnin_log.jsonl ──────────────────────────────────────────
    _set_bm(bm.replay_burnin_score,           "replay_burnin_score",           _DATA / "replay_burnin_log.jsonl",         "replay_burnin_score")
    _set_bm(bm.replay_continuity_score,       "replay_continuity_score",       _DATA / "replay_burnin_log.jsonl",         "replay_continuity_score")
    _set_bm(bm.replay_consistency_score,      "replay_consistency_score",      _DATA / "replay_burnin_log.jsonl",         "replay_consistency_score")

    # ── S-6: incident_noise_log.jsonl ─────────────────────────────────────────
    _set_bm(bm.incident_signal_quality_score, "incident_signal_quality_score", _DATA / "incident_noise_log.jsonl",        "incident_signal_quality_score")
    _set_bm(bm.alert_precision_score,         "alert_precision_score",         _DATA / "incident_noise_log.jsonl",        "alert_precision_score")
    _set_bm(bm.operational_noise_score,       "operational_noise_score",       _DATA / "incident_noise_log.jsonl",        "operational_noise_score")

    # ── S-7: cold_start_validation_log.jsonl ──────────────────────────────────
    _set_bm(bm.cold_start_resilience_score,   "cold_start_resilience_score",   _DATA / "cold_start_validation_log.jsonl", "cold_start_resilience_score")

    # ── S-8: operational_drift_log.jsonl ──────────────────────────────────────
    _set_bm(bm.operational_drift_score,       "operational_drift_score",       _DATA / "operational_drift_log.jsonl",     "operational_drift_score")
    _set_bm(bm.runtime_consistency_trend,     "runtime_consistency_trend",     _DATA / "operational_drift_log.jsonl",     "runtime_consistency_trend")
    _set_bm(bm.stability_trend_score,         "stability_trend_score",         _DATA / "operational_drift_log.jsonl",     "stability_trend_score")

    # ── S-9: runtime_stability_summary.jsonl ──────────────────────────────────
    _set_bm(bm.runtime_stability_score,           "runtime_stability_score",           _DATA / "runtime_stability_summary.jsonl",  "runtime_stability_score")
    _set_bm(bm.observability_readiness_score,     "observability_readiness_score",     _DATA / "runtime_stability_summary.jsonl",  "observability_readiness_score")
    _set_bm(bm.burnin_readiness_score,            "burnin_readiness_score",            _DATA / "runtime_stability_summary.jsonl",  "burnin_readiness_score")
    _set_bm(bm.burnin_operational_maturity_score, "burnin_operational_maturity_score", _DATA / "runtime_stability_summary.jsonl",  "burnin_operational_maturity_score")

    return updated


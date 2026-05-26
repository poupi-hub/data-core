"""
deployment_safety_validator.py — Phase R R-5

Pre-Deploy Safety Validator.

Valida todos os checks de seguranca antes de um deployment prosseguir.
Pontua o risco de deployment e recomenda ou bloqueia o procedimento.

Scores produzidos:
  - deployment_safety_score:   seguranca geral do deployment (0-100)
  - migration_integrity_score: integridade de migracoes e configuracoes (0-100)
  - rollback_risk_score:       risco de necessidade de rollback (0-100, 100=maior risco)
  - compatibility_score:       compatibilidade retroativa (0-100)

Checks (20 total):
  IMPORTS       (5): core, prometheus, sqlalchemy, fastapi, phase_q
  CONFIGS       (4): env_trading_mode, env_live_disabled, env_manual_confirm, data_directory
  REPLAY        (3): log_exists, readable, fidelity
  METRICS       (3): prometheus_importable, runtime_metrics_exists, metrics_consistency
  GOVERNANCE    (3): log_exists, freshness, guardian_state_safe
  COMPATIBILITY (2): state_schema_version, jsonl_readable

deploy_recommended = deployment_safety_score >= 70 and zero critical failures

CLI:
  python -m domains.crypto_coin.research.deployment_safety_validator
  python -m domains.crypto_coin.research.deployment_safety_validator --json
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALIDATION_LOG = Path("data/deployment_validation_log.jsonl")

# Source paths (read-only)
DATA_DIR             = Path("data")
REPLAY_LOG           = Path("data/live_execution_replay_log.jsonl")
GOV_SUMMARY_LOG      = Path("data/live_governance_summary.jsonl")
OPERATIONAL_STATE    = Path("data/operational_state.json")
RUNTIME_METRICS_PATH = Path("api/runtime_metrics.py")

# JSONLs considerados criticos para verificacao de leitura
CRITICAL_JSONLS = [
    Path("data/live_governance_summary.jsonl"),
    Path("data/governance_history.jsonl"),
    Path("data/live_readiness_revalidation_log.jsonl"),
]

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        deployment_safety_score   as _prom_deploy_safety,
        migration_integrity_score as _prom_migration,
        rollback_risk_score       as _prom_rollback_risk,
        compatibility_score       as _prom_compat,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ────────────────────────────────────────────────────────────────────

SAFE_GUARDIAN_STATES  = {"NORMAL", "MONITORING", "CONTRACTING"}
REQUIRED_SCHEMA_VER   = "1.0"
MIN_REPLAY_FIDELITY   = 0.70
MAX_GOVERNANCE_AGE_MIN = 60.0

PENALTY_CRITICAL = 25
PENALTY_HIGH     = 10
PENALTY_MEDIUM   = 3

DEPLOY_SCORE_THRESHOLD = 70.0
ROLLBACK_RISK_THRESHOLD = 40.0


# ── Data classes ─────────────────────────────────────────────────────────────────

@dataclass
class ValidationCheck:
    name:       str
    category:   str    # imports | migrations | configs | replay | metrics | governance | compatibility
    passed:     bool
    detail:     str
    risk_level: str    # low | medium | high | critical


@dataclass
class DeploymentSafetyReport:
    """Relatorio de validacao pre-deployment."""
    report_id:                str
    deployment_safety_score:  float   # 0-100
    migration_integrity_score: float  # 0-100
    rollback_risk_score:      float   # 0-100 (100 = maior risco)
    compatibility_score:      float   # 0-100
    checks:                   list[ValidationCheck]
    checks_passed:            int
    checks_failed:            int
    critical_failures:        list[str]
    high_risk_items:          list[str]
    deploy_recommended:       bool
    rollback_plan_required:   bool
    validation_summary:       str
    validated_at:             str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


# ── Validator ────────────────────────────────────────────────────────────────────

class DeploymentSafetyValidator:
    """
    R-5: Validador pre-deployment autonomo.

    Executa 20 checks de seguranca organizados em 6 categorias
    e computa scores de risco de deployment.
    """

    def __init__(self, validation_log: Path = VALIDATION_LOG):
        self.validation_log = validation_log

    def validate(self) -> DeploymentSafetyReport:
        """Executa todos os checks e retorna relatorio de seguranca."""
        report_id = str(uuid.uuid4())[:10]

        checks: list[ValidationCheck] = []

        # ── IMPORTS ──────────────────────────────────────────────────────────────
        checks.append(self._check_core_imports())
        checks.append(self._check_prometheus_imports())
        checks.append(self._check_sqlalchemy_imports())
        checks.append(self._check_fastapi_imports())
        checks.append(self._check_phase_q_imports())

        # ── CONFIGS ──────────────────────────────────────────────────────────────
        checks.append(self._check_env_trading_mode())
        checks.append(self._check_env_live_disabled())
        checks.append(self._check_env_manual_confirm())
        checks.append(self._check_data_directory())

        # ── REPLAY ───────────────────────────────────────────────────────────────
        checks.append(self._check_replay_log_exists())
        checks.append(self._check_replay_readable())
        checks.append(self._check_replay_fidelity())

        # ── METRICS ──────────────────────────────────────────────────────────────
        checks.append(self._check_prometheus_importable())
        checks.append(self._check_runtime_metrics_exists())
        checks.append(self._check_metrics_consistency())

        # ── GOVERNANCE ───────────────────────────────────────────────────────────
        checks.append(self._check_governance_log_exists())
        checks.append(self._check_governance_fresh())
        checks.append(self._check_guardian_state_safe())

        # ── COMPATIBILITY ────────────────────────────────────────────────────────
        checks.append(self._check_state_schema_version())
        checks.append(self._check_jsonl_readable())

        # ── Scores ───────────────────────────────────────────────────────────────
        critical_failures = [c.name for c in checks if not c.passed and c.risk_level == "critical"]
        high_risk_items   = [c.name for c in checks if not c.passed and c.risk_level == "high"]
        medium_failures   = [c for c in checks if not c.passed and c.risk_level == "medium"]

        checks_passed = sum(1 for c in checks if c.passed)
        checks_failed = len(checks) - checks_passed

        deployment_safety_score = max(
            0.0,
            100.0
            - len(critical_failures) * PENALTY_CRITICAL
            - len(high_risk_items)   * PENALTY_HIGH
            - len(medium_failures)   * PENALTY_MEDIUM,
        )
        deployment_safety_score = round(deployment_safety_score, 1)

        migration_integrity_score = self._compute_migration_integrity(checks)
        rollback_risk_score       = self._compute_rollback_risk(critical_failures, high_risk_items, medium_failures)
        compatibility_score_val   = self._compute_compatibility(checks)

        deploy_recommended      = deployment_safety_score >= DEPLOY_SCORE_THRESHOLD and len(critical_failures) == 0
        rollback_plan_required  = rollback_risk_score >= ROLLBACK_RISK_THRESHOLD

        validation_summary = self._build_summary(
            deployment_safety_score, deploy_recommended,
            critical_failures, high_risk_items, checks_passed, len(checks),
        )

        report = DeploymentSafetyReport(
            report_id                 = report_id,
            deployment_safety_score   = deployment_safety_score,
            migration_integrity_score = round(migration_integrity_score, 1),
            rollback_risk_score       = round(rollback_risk_score, 1),
            compatibility_score       = round(compatibility_score_val, 1),
            checks                    = checks,
            checks_passed             = checks_passed,
            checks_failed             = checks_failed,
            critical_failures         = critical_failures,
            high_risk_items           = high_risk_items,
            deploy_recommended        = deploy_recommended,
            rollback_plan_required    = rollback_plan_required,
            validation_summary        = validation_summary,
            validated_at              = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        self._push_metrics(report)
        return report

    # ── IMPORTS checks ───────────────────────────────────────────────────────────

    def _check_core_imports(self) -> ValidationCheck:
        try:
            import json, pathlib, dataclasses  # noqa: F401
            return ValidationCheck(
                name="core_imports", category="imports",
                passed=True, detail="json, pathlib, dataclasses OK",
                risk_level="low",
            )
        except ImportError as e:
            return ValidationCheck(
                name="core_imports", category="imports",
                passed=False, detail=f"import falhou: {e}",
                risk_level="critical",
            )

    def _check_prometheus_imports(self) -> ValidationCheck:
        try:
            import prometheus_client  # noqa: F401
            return ValidationCheck(
                name="prometheus_imports", category="imports",
                passed=True, detail="prometheus_client disponivel",
                risk_level="low",
            )
        except ImportError:
            return ValidationCheck(
                name="prometheus_imports", category="imports",
                passed=False, detail="prometheus_client nao instalado (metricas desabilitadas)",
                risk_level="low",
            )

    def _check_sqlalchemy_imports(self) -> ValidationCheck:
        try:
            import sqlalchemy  # noqa: F401
            return ValidationCheck(
                name="sqlalchemy_imports", category="imports",
                passed=True, detail=f"sqlalchemy disponivel",
                risk_level="low",
            )
        except ImportError:
            return ValidationCheck(
                name="sqlalchemy_imports", category="imports",
                passed=False, detail="sqlalchemy nao instalado",
                risk_level="medium",
            )

    def _check_fastapi_imports(self) -> ValidationCheck:
        try:
            import fastapi  # noqa: F401
            return ValidationCheck(
                name="fastapi_imports", category="imports",
                passed=True, detail="fastapi disponivel",
                risk_level="low",
            )
        except ImportError:
            return ValidationCheck(
                name="fastapi_imports", category="imports",
                passed=False, detail="fastapi nao instalado",
                risk_level="medium",
            )

    def _check_phase_q_imports(self) -> ValidationCheck:
        gov_path = Path("domains/crypto_coin/research/autonomous_live_governance.py")
        if not gov_path.exists():
            return ValidationCheck(
                name="phase_q_imports", category="imports",
                passed=False,
                detail=f"arquivo nao encontrado: {gov_path}",
                risk_level="high",
            )
        try:
            from domains.crypto_coin.research.autonomous_live_governance import (  # noqa: F401
                AutonomousLiveGovernance,
            )
            return ValidationCheck(
                name="phase_q_imports", category="imports",
                passed=True, detail="autonomous_live_governance importado OK",
                risk_level="low",
            )
        except ImportError as e:
            return ValidationCheck(
                name="phase_q_imports", category="imports",
                passed=False, detail=f"import falhou: {e}",
                risk_level="high",
            )

    # ── CONFIGS checks ───────────────────────────────────────────────────────────

    def _check_env_trading_mode(self) -> ValidationCheck:
        val = os.environ.get("TRADING_MODE", "")
        if val == "paper":
            return ValidationCheck(
                name="env_trading_mode", category="configs",
                passed=True, detail="TRADING_MODE=paper (seguro)",
                risk_level="low",
            )
        detail = (
            f"TRADING_MODE='{val}' (esperado 'paper')"
            if val else "TRADING_MODE nao definido (esperado 'paper')"
        )
        return ValidationCheck(
            name="env_trading_mode", category="configs",
            passed=False, detail=detail,
            risk_level="critical",
        )

    def _check_env_live_disabled(self) -> ValidationCheck:
        val = os.environ.get("ALLOW_LIVE_AUTO_ACTIVATION", "false").lower()
        if val == "true":
            return ValidationCheck(
                name="env_live_disabled", category="configs",
                passed=False,
                detail="ALLOW_LIVE_AUTO_ACTIVATION=true — ativacao automatica live HABILITADA",
                risk_level="critical",
            )
        return ValidationCheck(
            name="env_live_disabled", category="configs",
            passed=True,
            detail=f"ALLOW_LIVE_AUTO_ACTIVATION={val!r} (ativacao automatica desabilitada)",
            risk_level="low",
        )

    def _check_env_manual_confirm(self) -> ValidationCheck:
        val = os.environ.get("REQUIRE_MANUAL_LIVE_CONFIRMATION", "false").lower()
        if val == "true":
            return ValidationCheck(
                name="env_manual_confirm", category="configs",
                passed=True, detail="REQUIRE_MANUAL_LIVE_CONFIRMATION=true (seguro)",
                risk_level="low",
            )
        return ValidationCheck(
            name="env_manual_confirm", category="configs",
            passed=False,
            detail=f"REQUIRE_MANUAL_LIVE_CONFIRMATION={val!r} (recomendado 'true')",
            risk_level="high",
        )

    def _check_data_directory(self) -> ValidationCheck:
        if DATA_DIR.exists() and DATA_DIR.is_dir():
            return ValidationCheck(
                name="data_directory", category="configs",
                passed=True, detail=f"diretorio {DATA_DIR} existe",
                risk_level="low",
            )
        return ValidationCheck(
            name="data_directory", category="configs",
            passed=False, detail=f"diretorio {DATA_DIR} nao encontrado",
            risk_level="critical",
        )

    # ── REPLAY checks ────────────────────────────────────────────────────────────

    def _check_replay_log_exists(self) -> ValidationCheck:
        if REPLAY_LOG.exists():
            return ValidationCheck(
                name="replay_log_exists", category="replay",
                passed=True, detail=f"{REPLAY_LOG} existe",
                risk_level="low",
            )
        return ValidationCheck(
            name="replay_log_exists", category="replay",
            passed=False, detail=f"{REPLAY_LOG} nao encontrado",
            risk_level="medium",
        )

    def _check_replay_readable(self) -> ValidationCheck:
        if not REPLAY_LOG.exists():
            return ValidationCheck(
                name="replay_readable", category="replay",
                passed=False, detail="replay log ausente — nao legivel",
                risk_level="medium",
            )
        try:
            last = self._read_last_line(REPLAY_LOG)
            if last:
                json.loads(last)
                return ValidationCheck(
                    name="replay_readable", category="replay",
                    passed=True, detail="ultima linha do replay log legivel e valida",
                    risk_level="low",
                )
            return ValidationCheck(
                name="replay_readable", category="replay",
                passed=False, detail="replay log vazio",
                risk_level="medium",
            )
        except Exception as e:
            return ValidationCheck(
                name="replay_readable", category="replay",
                passed=False, detail=f"erro ao ler replay log: {e}",
                risk_level="medium",
            )

    def _check_replay_fidelity(self) -> ValidationCheck:
        records = self._load_log(REPLAY_LOG, n=1)
        if not records:
            return ValidationCheck(
                name="replay_fidelity", category="replay",
                passed=False, detail="replay log ausente ou vazio",
                risk_level="high",
            )
        raw = records[-1].get("avg_fidelity_score", 0.0)
        fidelity = float(raw)
        # normaliza se necessario
        if fidelity > 1.0:
            fidelity /= 100.0
        if fidelity >= MIN_REPLAY_FIDELITY:
            return ValidationCheck(
                name="replay_fidelity", category="replay",
                passed=True,
                detail=f"avg_fidelity_score={fidelity:.3f} >= {MIN_REPLAY_FIDELITY}",
                risk_level="low",
            )
        return ValidationCheck(
            name="replay_fidelity", category="replay",
            passed=False,
            detail=f"avg_fidelity_score={fidelity:.3f} < {MIN_REPLAY_FIDELITY} (minimo)",
            risk_level="high",
        )

    # ── METRICS checks ───────────────────────────────────────────────────────────

    def _check_prometheus_importable(self) -> ValidationCheck:
        try:
            import prometheus_client  # noqa: F401
            return ValidationCheck(
                name="prometheus_importable", category="metrics",
                passed=True, detail="prometheus_client importavel",
                risk_level="low",
            )
        except ImportError:
            return ValidationCheck(
                name="prometheus_importable", category="metrics",
                passed=False, detail="prometheus_client nao disponivel (metricas desabilitadas)",
                risk_level="medium",
            )

    def _check_runtime_metrics_exists(self) -> ValidationCheck:
        if RUNTIME_METRICS_PATH.exists():
            return ValidationCheck(
                name="runtime_metrics_exists", category="metrics",
                passed=True, detail=f"{RUNTIME_METRICS_PATH} encontrado",
                risk_level="low",
            )
        return ValidationCheck(
            name="runtime_metrics_exists", category="metrics",
            passed=False, detail=f"{RUNTIME_METRICS_PATH} nao encontrado",
            risk_level="medium",
        )

    def _check_metrics_consistency(self) -> ValidationCheck:
        try:
            import api.runtime_metrics  # noqa: F401
            return ValidationCheck(
                name="metrics_consistency", category="metrics",
                passed=True, detail="api.runtime_metrics importavel",
                risk_level="low",
            )
        except ImportError as e:
            return ValidationCheck(
                name="metrics_consistency", category="metrics",
                passed=False, detail=f"api.runtime_metrics nao importavel: {e}",
                risk_level="medium",
            )

    # ── GOVERNANCE checks ─────────────────────────────────────────────────────────

    def _check_governance_log_exists(self) -> ValidationCheck:
        if GOV_SUMMARY_LOG.exists():
            return ValidationCheck(
                name="governance_log_exists", category="governance",
                passed=True, detail=f"{GOV_SUMMARY_LOG} existe",
                risk_level="low",
            )
        return ValidationCheck(
            name="governance_log_exists", category="governance",
            passed=False, detail=f"{GOV_SUMMARY_LOG} nao encontrado",
            risk_level="high",
        )

    def _check_governance_fresh(self) -> ValidationCheck:
        if not GOV_SUMMARY_LOG.exists():
            return ValidationCheck(
                name="governance_fresh", category="governance",
                passed=False, detail="governance log ausente",
                risk_level="medium",
            )
        try:
            mtime   = GOV_SUMMARY_LOG.stat().st_mtime
            now     = datetime.now(timezone.utc).timestamp()
            age_min = (now - mtime) / 60.0
            if age_min <= MAX_GOVERNANCE_AGE_MIN:
                return ValidationCheck(
                    name="governance_fresh", category="governance",
                    passed=True,
                    detail=f"governance log atualizado ha {age_min:.0f} min",
                    risk_level="low",
                )
            return ValidationCheck(
                name="governance_fresh", category="governance",
                passed=False,
                detail=f"governance log desatualizado: {age_min:.0f} min (max={MAX_GOVERNANCE_AGE_MIN:.0f})",
                risk_level="medium",
            )
        except Exception as e:
            return ValidationCheck(
                name="governance_fresh", category="governance",
                passed=False, detail=f"erro ao verificar mtime: {e}",
                risk_level="medium",
            )

    def _check_guardian_state_safe(self) -> ValidationCheck:
        records = self._load_log(GOV_SUMMARY_LOG, n=1)
        if not records:
            return ValidationCheck(
                name="guardian_state_safe", category="governance",
                passed=False, detail="nenhum registro de governance encontrado",
                risk_level="high",
            )
        state = records[-1].get("guardian_state", "UNKNOWN")
        if state in SAFE_GUARDIAN_STATES:
            return ValidationCheck(
                name="guardian_state_safe", category="governance",
                passed=True, detail=f"guardian_state={state} (seguro)",
                risk_level="low",
            )
        return ValidationCheck(
            name="guardian_state_safe", category="governance",
            passed=False,
            detail=f"guardian_state={state} — estado nao seguro para deployment",
            risk_level="high",
        )

    # ── COMPATIBILITY checks ─────────────────────────────────────────────────────

    def _check_state_schema_version(self) -> ValidationCheck:
        if not OPERATIONAL_STATE.exists():
            return ValidationCheck(
                name="state_schema_version", category="compatibility",
                passed=False, detail=f"{OPERATIONAL_STATE} nao encontrado",
                risk_level="medium",
            )
        try:
            with open(OPERATIONAL_STATE) as f:
                state = json.load(f)
            version = str(state.get("schema_version", ""))
            if version == REQUIRED_SCHEMA_VER:
                return ValidationCheck(
                    name="state_schema_version", category="compatibility",
                    passed=True,
                    detail=f"schema_version={version!r} (compativel)",
                    risk_level="low",
                )
            return ValidationCheck(
                name="state_schema_version", category="compatibility",
                passed=False,
                detail=f"schema_version={version!r} (esperado {REQUIRED_SCHEMA_VER!r})",
                risk_level="medium",
            )
        except Exception as e:
            return ValidationCheck(
                name="state_schema_version", category="compatibility",
                passed=False, detail=f"erro ao ler {OPERATIONAL_STATE}: {e}",
                risk_level="medium",
            )

    def _check_jsonl_readable(self) -> ValidationCheck:
        failures: list[str] = []
        for path in CRITICAL_JSONLS:
            if not path.exists():
                continue   # ausencia e verificada por outros checks
            try:
                last = self._read_last_line(path)
                if last:
                    json.loads(last)
            except Exception as e:
                failures.append(f"{path.name}: {e}")
        if not failures:
            return ValidationCheck(
                name="jsonl_readable", category="compatibility",
                passed=True, detail="todos os JSONLs criticos legiveis",
                risk_level="low",
            )
        return ValidationCheck(
            name="jsonl_readable", category="compatibility",
            passed=False,
            detail=f"JSONLs com erro: {'; '.join(failures)}",
            risk_level="high",
        )

    # ── Score helpers ────────────────────────────────────────────────────────────

    def _compute_migration_integrity(self, checks: list[ValidationCheck]) -> float:
        """Baseado em checks de configs + data_directory."""
        config_checks = [c for c in checks if c.category == "configs"]
        if not config_checks:
            return 100.0
        passed = sum(1 for c in config_checks if c.passed)
        return (passed / len(config_checks)) * 100.0

    def _compute_rollback_risk(
        self,
        critical: list[str],
        high:     list[str],
        medium:   list[ValidationCheck],
    ) -> float:
        if critical:
            return 100.0
        if high:
            return 60.0
        if medium:
            return 30.0
        return 0.0

    def _compute_compatibility(self, checks: list[ValidationCheck]) -> float:
        """Baseado em checks de compatibility + state_schema_version."""
        compat_checks = [c for c in checks if c.category == "compatibility"]
        if not compat_checks:
            return 100.0
        passed = sum(1 for c in compat_checks if c.passed)
        return (passed / len(compat_checks)) * 100.0

    def _build_summary(
        self,
        score:    float,
        deploy:   bool,
        critical: list[str],
        high:     list[str],
        passed:   int,
        total:    int,
    ) -> str:
        verdict = "DEPLOY RECOMENDADO" if deploy else "DEPLOY BLOQUEADO"
        parts = [f"{verdict} | score={score:.0f}/100 | checks={passed}/{total}"]
        if critical:
            parts.append(f"CRITICOS: {', '.join(critical)}")
        if high:
            parts.append(f"HIGH: {', '.join(high)}")
        return " | ".join(parts)

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _persist(self, report: DeploymentSafetyReport) -> None:
        try:
            self.validation_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.validation_log, "a") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

    def _push_metrics(self, report: DeploymentSafetyReport) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_deploy_safety.set(report.deployment_safety_score)
            _prom_migration.set(report.migration_integrity_score)
            _prom_rollback_risk.set(report.rollback_risk_score)
            _prom_compat.set(report.compatibility_score)
        except Exception:
            pass

    # ── IO helpers ───────────────────────────────────────────────────────────────

    def _read_last_line(self, path: Path) -> str:
        last = ""
        try:
            with open(path) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        last = stripped
        except Exception:
            pass
        return last

    def _load_log(self, path: Path, n: int = 5) -> list[dict]:
        if not path.exists():
            return []
        records: list[dict] = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return records[-n:]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-Deploy Safety Validator — Phase R R-5"
    )
    parser.add_argument("--json", action="store_true", help="Saida JSON")
    args = parser.parse_args()

    validator = DeploymentSafetyValidator()
    report    = validator.validate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    verdict_icon = "[GO]" if report.deploy_recommended else "[NO]"
    print(f"\nPre-Deploy Safety Validator — Phase R R-5")
    print(f"  report_id:                {report.report_id}")
    print(f"  deployment_safety_score:  {report.deployment_safety_score:.1f}/100")
    print(f"  migration_integrity:      {report.migration_integrity_score:.1f}/100")
    print(f"  rollback_risk_score:      {report.rollback_risk_score:.1f}/100")
    print(f"  compatibility_score:      {report.compatibility_score:.1f}/100")
    print(f"  deploy_recommended:       {verdict_icon} {'SIM' if report.deploy_recommended else 'NAO'}")
    print(f"  rollback_plan_required:   {'SIM' if report.rollback_plan_required else 'nao'}")
    print(f"  checks: {report.checks_passed}/{report.checks_passed + report.checks_failed} OK")

    if report.critical_failures:
        print(f"\n  [CRITICO] {', '.join(report.critical_failures)}")
    if report.high_risk_items:
        print(f"  [HIGH]    {', '.join(report.high_risk_items)}")

    # Agrupa checks por categoria
    categories: dict[str, list[ValidationCheck]] = {}
    for c in report.checks:
        categories.setdefault(c.category, []).append(c)

    print(f"\n  Checks por categoria:")
    for cat, cat_checks in categories.items():
        for c in cat_checks:
            status = "OK " if c.passed else "NOK"
            risk   = f"[{c.risk_level}]" if not c.passed else ""
            print(f"    [{status}] {c.name:<30} {risk} {c.detail}")

    print(f"\n  -> {report.validation_summary}")
    print(f"\n  validated_at: {report.validated_at}")


if __name__ == "__main__":
    main()

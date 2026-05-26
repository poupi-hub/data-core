"""
autonomous_startup_manager.py — Phase R R-1

Boot Orchestration Manager.

Valida o ambiente, restaura estado e inicializa todos os subsistemas
na ordem correta antes de permitir operacao autonoma.

Scores produzidos:
  - startup_health_score:     saude geral da inicializacao (0-100)
  - startup_integrity_score:  integridade e seguranca da inicializacao (0-100)

Fases de inicializacao (em ordem):
  1. validate_configs        — variaveis de ambiente obrigatorias
  2. validate_postgres       — driver sqlalchemy + diretorio data/
  3. validate_redis          — REDIS_URL presente
  4. validate_exchange       — EXCHANGE_API_KEY ou BINANCE_API_KEY
  5. validate_prometheus     — import prometheus_client
  6. validate_grafana         — GRAFANA_URL presente
  7. validate_replay_storage  — data/ existe e e gravavel
  8. restore_runtime_state    — le data/runtime_state.json
  9. bootstrap_governance     — data/live_governance_summary.jsonl existe
  10. bootstrap_analytics     — data/governance_history.jsonl existe
  11. bootstrap_collectors    — data/live_execution_audit_log.jsonl existe
  12. bootstrap_paper_execution — TRADING_MODE=paper ou live_auto_activation=false

CLI:
  python -m domains.crypto_coin.research.autonomous_startup_manager
  python -m domains.crypto_coin.research.autonomous_startup_manager --json
  python -m domains.crypto_coin.research.autonomous_startup_manager --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STARTUP_LOG    = Path("data/startup_log.jsonl")
RUNTIME_STATE  = Path("data/runtime_state.json")

# Source files checked during bootstrap
LIVE_GOV_SUMMARY  = Path("data/live_governance_summary.jsonl")
GOVERNANCE_HIST   = Path("data/governance_history.jsonl")
AUDIT_LOG         = Path("data/live_execution_audit_log.jsonl")

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        startup_health_score     as _prom_health,
        startup_integrity_score  as _prom_integrity,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Critical checks (failures penalize integrity more) ────────────────────────

CRITICAL_CHECKS = {"validate_postgres", "validate_replay_storage"}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class EnvironmentCheck:
    """Resultado de uma verificacao de ambiente individual."""
    name:       str
    passed:     bool
    detail:     str
    latency_ms: float


@dataclass
class StartupReport:
    """Relatorio completo de inicializacao do sistema."""
    report_id:                   str
    startup_health_score:        float   # 0-100
    startup_integrity_score:     float   # 0-100
    startup_phase:               str     # VALIDATING | RESTORING | BOOTSTRAPPING | READY | FAILED
    startup_recovery_state:      str     # COLD_START | WARM_RESTORE | PARTIAL_RESTORE | FAILED
    checks:                      list[EnvironmentCheck]
    checks_passed:               int
    checks_total:                int
    failed_checks:               list[str]
    trading_mode:                str     # paper | live
    live_auto_activation_allowed: bool
    started_at:                  str
    duration_ms:                 float
    recommendation:              str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


# ── Manager ───────────────────────────────────────────────────────────────────

class AutonomousStartupManager:
    """
    R-1: Orquestrador de boot autonomo.

    Executa todas as verificacoes de ambiente e bootstrap de subsistemas
    na ordem correta. Determina recovery_state e bloqueia live se integridade
    insuficiente.
    """

    def __init__(
        self,
        startup_log:   Path = STARTUP_LOG,
        runtime_state: Path = RUNTIME_STATE,
        dry_run:       bool = False,
    ):
        self.startup_log   = startup_log
        self.runtime_state = runtime_state
        self.dry_run       = dry_run

    def run(self) -> StartupReport:
        """Executa sequencia completa de boot e retorna relatorio."""
        report_id = str(uuid.uuid4())[:12]
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.time() * 1000

        checks: list[EnvironmentCheck] = []

        # ── Phase 1: VALIDATING ───────────────────────────────────────────────
        current_phase = "VALIDATING"
        checks.append(self._run_check("validate_configs",          self._validate_configs))
        checks.append(self._run_check("validate_postgres",         self._validate_postgres))
        checks.append(self._run_check("validate_redis",            self._validate_redis))
        checks.append(self._run_check("validate_exchange",         self._validate_exchange))
        checks.append(self._run_check("validate_prometheus",       self._validate_prometheus))
        checks.append(self._run_check("validate_grafana",          self._validate_grafana))
        checks.append(self._run_check("validate_replay_storage",   self._validate_replay_storage))

        # ── Phase 2: RESTORING ────────────────────────────────────────────────
        current_phase = "RESTORING"
        checks.append(self._run_check("restore_runtime_state",     self._restore_runtime_state))

        # ── Phase 3: BOOTSTRAPPING ────────────────────────────────────────────
        current_phase = "BOOTSTRAPPING"
        checks.append(self._run_check("bootstrap_governance",      self._bootstrap_governance))
        checks.append(self._run_check("bootstrap_analytics",       self._bootstrap_analytics))
        checks.append(self._run_check("bootstrap_collectors",      self._bootstrap_collectors))
        checks.append(self._run_check("bootstrap_paper_execution", self._bootstrap_paper_execution))

        # ── Scoring ───────────────────────────────────────────────────────────
        checks_passed = sum(1 for c in checks if c.passed)
        checks_total  = len(checks)
        failed_checks = [c.name for c in checks if not c.passed]

        health_score    = (checks_passed / checks_total) * 100.0 if checks_total else 0.0
        health_score    = round(health_score, 1)

        integrity_score = self._compute_integrity_score(checks, failed_checks)

        # ── Derive operational values ─────────────────────────────────────────
        trading_mode = os.environ.get("TRADING_MODE", "paper").lower()
        live_auto    = os.environ.get("ALLOW_LIVE_AUTO_ACTIVATION", "false").lower() == "true"

        # ── Recovery state ────────────────────────────────────────────────────
        recovery_state = self._compute_recovery_state(failed_checks)

        # ── Final phase ───────────────────────────────────────────────────────
        has_critical_failure = any(n in CRITICAL_CHECKS for n in failed_checks)
        if has_critical_failure or integrity_score < 30.0:
            startup_phase = "FAILED"
        elif checks_passed == checks_total:
            startup_phase = "READY"
        else:
            startup_phase = "BOOTSTRAPPING"

        duration_ms = round(time.time() * 1000 - t0, 1)

        recommendation = self._build_recommendation(
            health_score, integrity_score, startup_phase,
            failed_checks, trading_mode, live_auto,
        )

        report = StartupReport(
            report_id                    = report_id,
            startup_health_score         = health_score,
            startup_integrity_score      = integrity_score,
            startup_phase                = startup_phase,
            startup_recovery_state       = recovery_state,
            checks                       = checks,
            checks_passed                = checks_passed,
            checks_total                 = checks_total,
            failed_checks                = failed_checks,
            trading_mode                 = trading_mode,
            live_auto_activation_allowed = live_auto,
            started_at                   = started_at,
            duration_ms                  = duration_ms,
            recommendation               = recommendation,
        )

        if not self.dry_run:
            self._persist(report)
            self._persist_runtime_state(report)

        if _METRICS_AVAILABLE:
            try:
                _prom_health.set(health_score)
                _prom_integrity.set(integrity_score)
            except Exception:
                pass

        return report

    # ── Individual checks ─────────────────────────────────────────────────────

    def _validate_configs(self) -> tuple[bool, str]:
        """Verifica variaveis de ambiente obrigatorias."""
        required = ["BOT_AUTO_START", "TRADING_MODE"]
        optional = ["ALLOW_LIVE_AUTO_ACTIVATION", "REQUIRE_MANUAL_LIVE_CONFIRMATION"]
        missing_required = [v for v in required if not os.environ.get(v)]
        present_optional = [v for v in optional if os.environ.get(v)]
        if missing_required:
            return False, f"Env vars ausentes: {', '.join(missing_required)}"
        detail = f"Required OK. Optional present: {len(present_optional)}/{len(optional)}"
        return True, detail

    def _validate_postgres(self) -> tuple[bool, str]:
        """Verifica driver sqlalchemy e diretorio data/."""
        try:
            import sqlalchemy  # noqa: F401
            sql_ok = True
        except ImportError:
            sql_ok = False
        data_ok = Path("data").is_dir()
        if not sql_ok and not data_ok:
            return False, "sqlalchemy nao instalado; data/ nao existe"
        if not sql_ok:
            return False, "sqlalchemy nao instalado (instale: pip install sqlalchemy)"
        if not data_ok:
            return False, "Diretorio data/ nao encontrado"
        return True, "sqlalchemy OK; data/ existe"

    def _validate_redis(self) -> tuple[bool, str]:
        """Verifica REDIS_URL no ambiente."""
        url = os.environ.get("REDIS_URL", "")
        if not url:
            return False, "REDIS_URL nao definido"
        masked = url[:20] + "..." if len(url) > 20 else url
        return True, f"REDIS_URL presente ({masked})"

    def _validate_exchange(self) -> tuple[bool, str]:
        """Verifica credenciais de exchange."""
        key = os.environ.get("EXCHANGE_API_KEY") or os.environ.get("BINANCE_API_KEY")
        if not key:
            return False, "EXCHANGE_API_KEY e BINANCE_API_KEY ausentes"
        src = "EXCHANGE_API_KEY" if os.environ.get("EXCHANGE_API_KEY") else "BINANCE_API_KEY"
        return True, f"{src} presente"

    def _validate_prometheus(self) -> tuple[bool, str]:
        """Verifica disponibilidade do prometheus_client."""
        try:
            import prometheus_client  # noqa: F401
            return True, "prometheus_client disponivel"
        except ImportError:
            return False, "prometheus_client nao instalado (metricas desabilitadas)"

    def _validate_grafana(self) -> tuple[bool, str]:
        """Verifica GRAFANA_URL no ambiente."""
        url = os.environ.get("GRAFANA_URL", "")
        if not url:
            return False, "GRAFANA_URL nao definido (dashboards indisponiveis)"
        return True, f"GRAFANA_URL presente"

    def _validate_replay_storage(self) -> tuple[bool, str]:
        """Verifica que data/ existe e e gravavel."""
        data_dir = Path("data")
        if not data_dir.exists():
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return False, f"Nao foi possivel criar data/: {exc}"
        test_file = data_dir / ".write_test"
        try:
            test_file.write_text("ok")
            test_file.unlink()
        except Exception as exc:
            return False, f"data/ nao e gravavel: {exc}"
        return True, "data/ existe e e gravavel"

    def _restore_runtime_state(self) -> tuple[bool, str]:
        """Tenta restaurar estado anterior de data/runtime_state.json."""
        if not self.runtime_state.exists():
            return True, "runtime_state.json ausente — COLD_START"
        try:
            raw = self.runtime_state.read_text(encoding="utf-8")
            state = json.loads(raw)
            phase = state.get("startup_phase", "unknown")
            ts    = state.get("persisted_at", "unknown")
            return True, f"Estado restaurado: phase={phase} ts={ts}"
        except Exception as exc:
            return False, f"Falha ao ler runtime_state.json: {exc}"

    def _bootstrap_governance(self) -> tuple[bool, str]:
        """Verifica existencia do log de governanca live."""
        if LIVE_GOV_SUMMARY.exists():
            size = LIVE_GOV_SUMMARY.stat().st_size
            return True, f"live_governance_summary.jsonl OK ({size} bytes)"
        return False, "live_governance_summary.jsonl nao encontrado (governanca nao iniciada)"

    def _bootstrap_analytics(self) -> tuple[bool, str]:
        """Verifica existencia do historico de governanca."""
        if GOVERNANCE_HIST.exists():
            size = GOVERNANCE_HIST.stat().st_size
            return True, f"governance_history.jsonl OK ({size} bytes)"
        return False, "governance_history.jsonl nao encontrado (analytics sem historico)"

    def _bootstrap_collectors(self) -> tuple[bool, str]:
        """Verifica existencia do audit log de execucao."""
        if AUDIT_LOG.exists():
            size = AUDIT_LOG.stat().st_size
            return True, f"live_execution_audit_log.jsonl OK ({size} bytes)"
        return False, "live_execution_audit_log.jsonl nao encontrado (collectors nao ativos)"

    def _bootstrap_paper_execution(self) -> tuple[bool, str]:
        """Valida que modo paper esta ativo ou live auto-activation esta desabilitado."""
        mode     = os.environ.get("TRADING_MODE", "paper").lower()
        live_auto = os.environ.get("ALLOW_LIVE_AUTO_ACTIVATION", "false").lower() == "true"
        if mode == "paper":
            return True, "TRADING_MODE=paper — modo seguro confirmado"
        if not live_auto:
            return True, f"TRADING_MODE={mode} mas ALLOW_LIVE_AUTO_ACTIVATION=false — seguro"
        return False, (
            f"TRADING_MODE={mode} e ALLOW_LIVE_AUTO_ACTIVATION=true — "
            "ativacao automatica de live requer confirmacao manual"
        )

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _compute_integrity_score(
        self,
        checks: list[EnvironmentCheck],
        failed_checks: list[str],
    ) -> float:
        score = 100.0

        # Penaliza live auto activation (risco de seguranca)
        if os.environ.get("ALLOW_LIVE_AUTO_ACTIVATION", "false").lower() == "true":
            score -= 20.0

        # Penaliza TRADING_MODE != paper
        if os.environ.get("TRADING_MODE", "paper").lower() != "paper":
            score -= 15.0

        # Penaliza cada check critico falhado
        for name in failed_checks:
            if name in CRITICAL_CHECKS:
                score -= 10.0

        return round(max(0.0, min(100.0, score)), 1)

    def _compute_recovery_state(self, failed_checks: list[str]) -> str:
        """Determina tipo de recuperacao baseado no estado do runtime_state.json."""
        state_exists = self.runtime_state.exists()
        if not state_exists:
            return "COLD_START"

        # Tenta validar o conteudo
        try:
            raw   = self.runtime_state.read_text(encoding="utf-8")
            state = json.loads(raw)
            _ = state.get("startup_phase")  # sanidade basica
        except Exception:
            return "FAILED"

        if failed_checks:
            return "PARTIAL_RESTORE"
        return "WARM_RESTORE"

    def _build_recommendation(
        self,
        health: float,
        integrity: float,
        phase: str,
        failed: list[str],
        mode: str,
        live_auto: bool,
    ) -> str:
        if phase == "FAILED":
            return (
                f"BOOT FALHOU (health={health:.0f} integrity={integrity:.0f}). "
                f"Checks criticos falhados: {', '.join(failed)}. "
                "Corrigir antes de reiniciar."
            )
        if phase == "READY" and mode == "paper" and not live_auto:
            return (
                f"Boot completo (health={health:.0f} integrity={integrity:.0f}). "
                "TRADING_MODE=paper confirmado. Sistema pronto para operacao."
            )
        if failed:
            return (
                f"Boot parcial (health={health:.0f} integrity={integrity:.0f}). "
                f"Checks falhados (nao criticos): {', '.join(failed)}. "
                "Sistema operacional com funcionalidade reduzida."
            )
        if live_auto and mode != "paper":
            return (
                f"Boot OK mas live_auto_activation=True com mode={mode}. "
                "Requer confirmacao manual antes de ativar execucao live."
            )
        return (
            f"Boot bem-sucedido (health={health:.0f} integrity={integrity:.0f}). "
            "Todos os subsistemas inicializados."
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _run_check(self, name: str, fn) -> EnvironmentCheck:
        """Executa um check individual com medicao de latencia."""
        t0 = time.time() * 1000
        try:
            passed, detail = fn()
        except Exception as exc:
            passed = False
            detail = f"Excecao inesperada: {exc}"
        latency = round(time.time() * 1000 - t0, 2)
        return EnvironmentCheck(name=name, passed=passed, detail=detail, latency_ms=latency)

    def _persist(self, report: StartupReport) -> None:
        try:
            self.startup_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.startup_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

    def _persist_runtime_state(self, report: StartupReport) -> None:
        """Persiste fase atual em runtime_state.json."""
        try:
            self.runtime_state.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if self.runtime_state.exists():
                try:
                    existing = json.loads(self.runtime_state.read_text(encoding="utf-8"))
                except Exception:
                    pass
            existing.update({
                "persisted_at":          report.started_at,
                "startup_phase":         report.startup_phase,
                "startup_recovery_state": report.startup_recovery_state,
                "trading_mode":          report.trading_mode,
                "live_auto_activation":  report.live_auto_activation_allowed,
                "startup_health_score":  report.startup_health_score,
                "startup_integrity_score": report.startup_integrity_score,
            })
            self.runtime_state.write_text(
                json.dumps(existing, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Startup Manager — Phase R R-1"
    )
    parser.add_argument(
        "--json",    action="store_true",
        help="Saida em JSON",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Executa verificacoes sem persistir nada",
    )
    args = parser.parse_args()

    mgr    = AutonomousStartupManager(dry_run=args.dry_run)
    report = mgr.run()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    phase_icon = {
        "READY":        "[OK]",
        "BOOTSTRAPPING": "[~~]",
        "FAILED":       "[XX]",
        "VALIDATING":   "[..]",
        "RESTORING":    "[<<]",
    }.get(report.startup_phase, "[??]")

    recovery_icon = {
        "COLD_START":     "[COLD]",
        "WARM_RESTORE":   "[WARM]",
        "PARTIAL_RESTORE": "[PART]",
        "FAILED":         "[FAIL]",
    }.get(report.startup_recovery_state, "[??]")

    print(f"\nAutonomous Startup Manager — Phase R R-1")
    print(f"  report_id:               {report.report_id}")
    print(f"  startup_phase:           {phase_icon} {report.startup_phase}")
    print(f"  recovery_state:          {recovery_icon} {report.startup_recovery_state}")
    print(f"  startup_health_score:    {report.startup_health_score:.1f}/100")
    print(f"  startup_integrity_score: {report.startup_integrity_score:.1f}/100")
    print(f"  checks:                  {report.checks_passed}/{report.checks_total} OK")
    print(f"  trading_mode:            {report.trading_mode}")
    print(f"  live_auto_activation:    {'SIM (atencao)' if report.live_auto_activation_allowed else 'nao'}")
    print(f"  duration_ms:             {report.duration_ms:.1f}ms")

    if report.failed_checks:
        print(f"\n  Checks falhados ({len(report.failed_checks)}):")
        for c in report.checks:
            if not c.passed:
                print(f"    [FAIL] {c.name}: {c.detail}")

    print(f"\n  Checks executados:")
    for c in report.checks:
        icon = "[OK]" if c.passed else "[--]"
        print(f"    {icon} {c.name:<30} ({c.latency_ms:.1f}ms)")

    print(f"\n  -> {report.recommendation}")

    if args.dry_run:
        print("\n  [DRY-RUN] Nenhum dado persistido.")


if __name__ == "__main__":
    main()

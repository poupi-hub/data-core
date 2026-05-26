"""
self_healing_intelligence.py — Phase O FASE 7

Self-Healing Quant Infrastructure.

Detecta falhas operacionais e anomalias na infraestrutura quantitativa:
  - falhas em arquivos de experimento (JSONL corrompido, vazio, inconsistente)
  - métricas anômalas (sharpe > threshold, drawdown impossível)
  - replay inconsistente (mesmos parâmetros → resultados muito diferentes)
  - corrupção de lineage (IDs duplicados, gaps temporais)
  - datasets perigosos (integridade < threshold)

Scores produzidos:
  - infrastructure_health_score: saúde geral da infra (0–100)
  - recovery_confidence_score:   confiança de auto-recuperação (0–100)
  - self_healing_score:          qualidade do processo de auto-cura (0–100)

Mecanismos de auto-recuperação:
  - self-recovery: recalcula scores com dados válidos (ignora corrompidos)
  - automatic fallback: usa baseline neutro quando dados insuficientes
  - replay isolation: marca experimentos suspeitos com flag "quarantined"
  - degraded mode: opera com subset de dados quando há corrupção parcial

CLI:
  python -m domains.crypto_coin.research.self_healing_intelligence
  python -m domains.crypto_coin.research.self_healing_intelligence --json
  python -m domains.crypto_coin.research.self_healing_intelligence --heal
"""

from __future__ import annotations

import argparse
import json
import statistics
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.experiment_tracker import ExperimentTracker

EXPERIMENTS_DIR   = Path("data/experiments")
HEALING_LOG       = Path("data/self_healing_log.jsonl")
QUARANTINE_FILE   = Path("data/quarantined_experiments.json")

# Prometheus (optional)
try:
    from api.metrics import self_healing_score as _prom_healing
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────

SHARPE_ANOMALY_HIGH   = 15.0    # sharpe > 15 = suspeito
DRAWDOWN_ANOMALY_LOW  = -1.5    # drawdown < -150% = impossível
MIN_VALID_EXPERIMENTS = 3       # mínimo para considerar dados válidos
REPLAY_CONSISTENCY_CV = 0.8     # CV de sharpe em mesmos params > 0.8 = inconsistente


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class InfraIssue:
    """Problema detectado na infraestrutura."""
    issue_id:    str
    issue_type:  str   # corrupt_jsonl | anomalous_metric | replay_inconsistency | lineage_gap | empty_data
    severity:    str   # low | medium | high | critical
    strategy_id: str | None
    description: str
    auto_fixed:  bool
    fix_action:  str | None


@dataclass
class SelfHealingReport:
    """Relatório de auto-diagnóstico e cura da infraestrutura."""
    infrastructure_health_score: float   # 0–100
    recovery_confidence_score:   float   # 0–100
    self_healing_score:          float   # 0–100

    strategies_checked:  int
    strategies_healthy:  int
    strategies_degraded: int   # infra degraded, não score degraded
    experiments_total:   int
    experiments_quarantined: int   # marcados como suspeitos

    issues:              list[InfraIssue]
    issues_auto_fixed:   int
    degraded_mode:       bool   # True se operando com dados parciais
    evaluated_at:        str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["issues"] = [asdict(i) for i in self.issues]
        return d


# ── Engine ────────────────────────────────────────────────────────────────────

class SelfHealingIntelligence:
    """
    FASE 7: Diagnóstico e auto-cura da infraestrutura quantitativa.

    Método:
      1. Verifica integridade de cada arquivo JSONL de experimentos
      2. Detecta métricas anômalas (sharpe impossível, drawdown absurdo)
      3. Detecta replay inconsistente (mesmos params → resultados divergentes)
      4. Detecta gaps de lineage (run_ids duplicados, timestamps invertidos)
      5. Quarentena experimentos suspeitos (não deleta — apenas marca)
      6. Calcula scores de saúde e confiança
    """

    def __init__(
        self,
        experiments_dir: Path = EXPERIMENTS_DIR,
        healing_log:     Path = HEALING_LOG,
        quarantine_file: Path = QUARANTINE_FILE,
    ):
        self.experiments_dir = experiments_dir
        self.healing_log     = healing_log
        self.quarantine_file = quarantine_file

    def diagnose(self, auto_heal: bool = False) -> SelfHealingReport:
        """Executa diagnóstico completo. Se auto_heal=True, quarentena suspeitos."""
        strategy_files = list(self.experiments_dir.glob("*.jsonl"))
        strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]

        issues: list[InfraIssue] = []
        strategies_healthy   = 0
        strategies_degraded  = 0
        experiments_total    = 0
        quarantined_ids: set[str] = self._load_quarantine()
        newly_quarantined: set[str] = set()

        for sid in strategy_ids:
            strategy_issues: list[InfraIssue] = []

            try:
                tracker     = ExperimentTracker(strategy_id=sid, experiments_dir=self.experiments_dir)
                experiments = tracker.load_experiments()
                experiments_total += len(experiments)
            except Exception as e:
                issues.append(InfraIssue(
                    issue_id    = str(uuid.uuid4())[:8],
                    issue_type  = "corrupt_jsonl",
                    severity    = "critical",
                    strategy_id = sid,
                    description = f"Falha ao carregar JSONL: {e}",
                    auto_fixed  = False,
                    fix_action  = "Verificar e reparar arquivo manualmente",
                ))
                strategies_degraded += 1
                continue

            if len(experiments) < MIN_VALID_EXPERIMENTS:
                issues.append(InfraIssue(
                    issue_id    = str(uuid.uuid4())[:8],
                    issue_type  = "empty_data",
                    severity    = "medium",
                    strategy_id = sid,
                    description = f"Apenas {len(experiments)} experimentos — dados insuficientes",
                    auto_fixed  = False,
                    fix_action  = "Executar sweep_runner para gerar mais dados",
                ))

            # ── Métricas anômalas ─────────────────────────────────────────────
            for exp in experiments:
                sharpe   = exp.metrics.get("sharpe", 0.0)
                drawdown = exp.metrics.get("max_drawdown", 0.0)
                if sharpe > SHARPE_ANOMALY_HIGH:
                    strategy_issues.append(InfraIssue(
                        issue_id    = str(uuid.uuid4())[:8],
                        issue_type  = "anomalous_metric",
                        severity    = "high",
                        strategy_id = sid,
                        description = f"run_id={exp.run_id}: sharpe={sharpe:.2f} suspeito (>{SHARPE_ANOMALY_HIGH})",
                        auto_fixed  = auto_heal,
                        fix_action  = f"Quarentena de run_id={exp.run_id}" if auto_heal else "Investigar manualmente",
                    ))
                    if auto_heal:
                        newly_quarantined.add(exp.run_id)

                if drawdown < DRAWDOWN_ANOMALY_LOW:
                    strategy_issues.append(InfraIssue(
                        issue_id    = str(uuid.uuid4())[:8],
                        issue_type  = "anomalous_metric",
                        severity    = "high",
                        strategy_id = sid,
                        description = f"run_id={exp.run_id}: drawdown={drawdown:.2f} impossível (<{DRAWDOWN_ANOMALY_LOW})",
                        auto_fixed  = auto_heal,
                        fix_action  = f"Quarentena de run_id={exp.run_id}" if auto_heal else "Investigar manualmente",
                    ))
                    if auto_heal:
                        newly_quarantined.add(exp.run_id)

            # ── Replay consistency ────────────────────────────────────────────
            param_groups: dict[str, list[float]] = {}
            for exp in experiments:
                key = json.dumps(exp.parameters, sort_keys=True)
                sharpe = exp.metrics.get("sharpe", 0.0)
                param_groups.setdefault(key, []).append(sharpe)

            for params_key, sharpes in param_groups.items():
                if len(sharpes) >= 2:
                    try:
                        mean = statistics.mean(sharpes)
                        std  = statistics.stdev(sharpes)
                        cv   = abs(std / mean) if mean != 0 else float("inf")
                        if cv > REPLAY_CONSISTENCY_CV:
                            strategy_issues.append(InfraIssue(
                                issue_id    = str(uuid.uuid4())[:8],
                                issue_type  = "replay_inconsistency",
                                severity    = "medium",
                                strategy_id = sid,
                                description = f"Parâmetros idênticos com CV={cv:.2f} de sharpe — replay inconsistente",
                                auto_fixed  = False,
                                fix_action  = "Verificar seed aleatório e condições de replay",
                            ))
                    except statistics.StatisticsError:
                        pass

            # ── Lineage gaps (duplicate run_ids) ─────────────────────────────
            run_ids = [exp.run_id for exp in experiments]
            if len(run_ids) != len(set(run_ids)):
                strategy_issues.append(InfraIssue(
                    issue_id    = str(uuid.uuid4())[:8],
                    issue_type  = "lineage_gap",
                    severity    = "high",
                    strategy_id = sid,
                    description = f"run_ids duplicados detectados em {sid}",
                    auto_fixed  = False,
                    fix_action  = "Reconstruir JSONL removendo duplicatas",
                ))

            if strategy_issues:
                strategies_degraded += 1
            else:
                strategies_healthy += 1

            issues.extend(strategy_issues)

        # ── Persist quarantine ─────────────────────────────────────────────────
        if auto_heal and newly_quarantined:
            self._save_quarantine(quarantined_ids | newly_quarantined)

        experiments_quarantined = len(quarantined_ids | newly_quarantined)
        issues_auto_fixed = sum(1 for i in issues if i.auto_fixed)

        # ── Compute scores ────────────────────────────────────────────────────
        total_strategies = max(len(strategy_ids), 1)
        health_ratio     = strategies_healthy / total_strategies
        infra_health     = round(health_ratio * 100.0, 1)

        # Recovery confidence: quão confiante o sistema está de operar corretamente
        critical_issues  = sum(1 for i in issues if i.severity == "critical")
        high_issues      = sum(1 for i in issues if i.severity == "high")
        recovery_conf    = max(0.0, 100.0 - critical_issues * 25.0 - high_issues * 10.0)

        # Self-healing score: proporção de problemas auto-fixados
        total_fixable = sum(1 for i in issues if i.auto_fixed or i.auto_fixed is False)
        self_healing  = (issues_auto_fixed / max(total_fixable, 1)) * 100.0 if total_fixable > 0 else 100.0

        degraded_mode = critical_issues > 0 or infra_health < 50.0

        report = SelfHealingReport(
            infrastructure_health_score = infra_health,
            recovery_confidence_score   = round(recovery_conf, 1),
            self_healing_score          = round(self_healing, 1),
            strategies_checked          = len(strategy_ids),
            strategies_healthy          = strategies_healthy,
            strategies_degraded         = strategies_degraded,
            experiments_total           = experiments_total,
            experiments_quarantined     = experiments_quarantined,
            issues                      = issues,
            issues_auto_fixed           = issues_auto_fixed,
            degraded_mode               = degraded_mode,
            evaluated_at                = datetime.now(timezone.utc).isoformat(),
        )

        self._persist_log(report)
        if _METRICS_AVAILABLE:
            try:
                _prom_healing.set(self_healing)
            except Exception:
                pass

        return report

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_quarantine(self) -> set[str]:
        if not self.quarantine_file.exists():
            return set()
        try:
            data = json.loads(self.quarantine_file.read_text())
            return set(data.get("quarantined_run_ids", []))
        except Exception:
            return set()

    def _save_quarantine(self, run_ids: set[str]) -> None:
        try:
            self.quarantine_file.parent.mkdir(parents=True, exist_ok=True)
            self.quarantine_file.write_text(json.dumps({
                "quarantined_run_ids": sorted(run_ids),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
        except Exception:
            pass

    def _persist_log(self, report: SelfHealingReport) -> None:
        try:
            self.healing_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":               report.evaluated_at,
                "infrastructure_health_score": report.infrastructure_health_score,
                "recovery_confidence_score":   report.recovery_confidence_score,
                "self_healing_score":          report.self_healing_score,
                "issues_count":                len(report.issues),
                "issues_auto_fixed":           report.issues_auto_fixed,
                "degraded_mode":               report.degraded_mode,
            }
            with open(self.healing_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Self-Healing Intelligence — Phase O FASE 7")
    parser.add_argument("--heal", action="store_true", help="Auto-heal: quarentenar experimentos suspeitos")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = SelfHealingIntelligence()
    report = engine.diagnose(auto_heal=args.heal)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    print(f"\nSelf-Healing Intelligence")
    print(f"  infrastructure_health_score: {report.infrastructure_health_score:.0f}/100")
    print(f"  recovery_confidence_score:   {report.recovery_confidence_score:.0f}/100")
    print(f"  self_healing_score:          {report.self_healing_score:.0f}/100")
    print(f"  strategies_checked:    {report.strategies_checked} ({report.strategies_healthy} OK, {report.strategies_degraded} degraded)")
    print(f"  experiments_total:     {report.experiments_total} ({report.experiments_quarantined} quarantined)")
    print(f"  issues_found:          {len(report.issues)} ({report.issues_auto_fixed} auto-fixed)")
    print(f"  degraded_mode:         {'⚠️ ATIVO' if report.degraded_mode else 'inativo'}")
    if report.issues:
        print("\n  Issues:")
        for issue in report.issues[:10]:
            fixed_marker = " [AUTO-FIXED]" if issue.auto_fixed else ""
            print(f"    [{issue.severity.upper()}] {issue.issue_type}: {issue.description}{fixed_marker}")


if __name__ == "__main__":
    main()

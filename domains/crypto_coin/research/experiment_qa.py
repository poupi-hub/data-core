"""
experiment_qa.py — Phase I Fase 7

Controle de qualidade de experimentos registrados no ExperimentTracker.

Verifica:
  1. Completeness — campos obrigatórios presentes em todos os registros
  2. Plausibility — métricas dentro de limites razoáveis (sem NaN, sem infinito,
     drawdown ≤ 100%, sharpe entre -10 e 50, etc.)
  3. Parameter consistency — parâmetros do experimento batem com o StrategyRegistry
  4. Reproducibility flag — registros sem candles_count ou replay_dataset suspeitos
  5. Duplicate detection — run_ids duplicados ou experimentos idênticos (params + symbol + tf)

Saída:
  - QAReport por estratégia com lista de issues e score de qualidade 0–100
  - Formato texto para CLI, JSON para programático

CLI:
    python -m domains.crypto_coin.research.experiment_qa --all
    python -m domains.crypto_coin.research.experiment_qa --strategy trend_following
    python -m domains.crypto_coin.research.experiment_qa --strategy trend_following --json
    python -m domains.crypto_coin.research.experiment_qa --fix-duplicates --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from typing import Any

from .experiment_tracker import ExperimentTracker, ExperimentRecord, EXPERIMENTS_DIR
from .strategy_registry  import get_registry


# ── Thresholds de plausibilidade ──────────────────────────────────────────────

METRIC_BOUNDS: dict[str, tuple[float, float]] = {
    "sharpe":       (-10.0, 50.0),
    "sortino":      (-20.0, 100.0),
    "calmar":       (-10.0, 200.0),
    "max_drawdown": (-100.0, 0.0),   # negativo por convenção
    "expectancy":   (-100.0, 500.0),
    "win_rate":     (0.0, 1.0),
    "total_trades": (0.0, 100_000.0),
}

REQUIRED_METRICS  = {"sharpe", "max_drawdown", "total_trades"}
REQUIRED_FIELDS   = {"strategy_id", "symbol", "timeframe", "parameters", "metrics", "run_id"}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class QAIssue:
    run_id:   str
    level:    str    # 'error' | 'warning' | 'info'
    code:     str    # 'MISSING_FIELD' | 'IMPLAUSIBLE_METRIC' | 'PARAM_MISMATCH' | 'DUPLICATE' | ...
    message:  str
    field:    str | None = None


@dataclass
class QAReport:
    strategy_id:   str
    total_records: int
    issues:        list[QAIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "warning")

    @property
    def quality_score(self) -> float:
        """
        Score de qualidade 0–100.
        Penaliza erros (5pt cada) e warnings (1pt cada).
        """
        if self.total_records == 0:
            return 100.0
        penalty = self.error_count * 5.0 + self.warning_count * 1.0
        # Normaliza pelo número de registros para não penalizar datasets grandes
        normalized = penalty / max(self.total_records, 1) * 10
        return round(max(0.0, min(100.0, 100.0 - normalized)), 2)

    def summary(self) -> str:
        lines = [
            f"QA Report — {self.strategy_id}",
            f"  Registros   : {self.total_records}",
            f"  Erros       : {self.error_count}",
            f"  Warnings    : {self.warning_count}",
            f"  Quality     : {self.quality_score:.1f}/100",
        ]
        if self.issues:
            lines.append("\n  Issues:")
            for issue in self.issues[:20]:
                icon = "❌" if issue.level == "error" else "⚠️" if issue.level == "warning" else "ℹ️"
                field_tag = f"[{issue.field}] " if issue.field else ""
                lines.append(f"    {icon} [{issue.code}] {field_tag}{issue.message}")
            if len(self.issues) > 20:
                lines.append(f"    ... e mais {len(self.issues) - 20} issues")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "strategy_id":   self.strategy_id,
            "total_records": self.total_records,
            "error_count":   self.error_count,
            "warning_count": self.warning_count,
            "quality_score": self.quality_score,
            "issues": [
                {
                    "run_id":  i.run_id,
                    "level":   i.level,
                    "code":    i.code,
                    "message": i.message,
                    "field":   i.field,
                }
                for i in self.issues
            ],
        }


# ── QA Engine ─────────────────────────────────────────────────────────────────

class ExperimentQA:
    """
    Valida a qualidade dos experimentos registrados no ExperimentTracker.
    """

    def __init__(self) -> None:
        self.tracker  = ExperimentTracker()
        self.registry = get_registry()

    def run(self, strategy_id: str | None = None) -> list[QAReport]:
        """
        Executa QA completo.
        Se strategy_id for None, executa para todas as estratégias conhecidas.
        """
        if strategy_id:
            strategies = [strategy_id]
        else:
            # Descobrir estratégias dos arquivos JSONL existentes
            strategies = _discover_strategies()

        reports = []
        for sid in strategies:
            records = self.tracker.load_all(strategy_id=sid)
            report  = self._validate_strategy(sid, records)
            reports.append(report)
        return reports

    # ─── Validação por estratégia ──────────────────────────────────────────

    def _validate_strategy(
        self, strategy_id: str, records: list[ExperimentRecord]
    ) -> QAReport:
        report = QAReport(strategy_id=strategy_id, total_records=len(records))

        if not records:
            report.issues.append(QAIssue(
                run_id="N/A", level="info", code="NO_RECORDS",
                message=f"Nenhum experimento registrado para '{strategy_id}'",
            ))
            return report

        # Carregar parâmetros canônicos do StrategyRegistry (se disponível)
        canonical_params: dict[str, Any] | None = None
        try:
            canonical_params = self.registry.get_parameters(strategy_id)
        except Exception:
            pass

        seen_run_ids: set[str] = set()
        seen_signatures: dict[str, str] = {}  # signature → run_id

        for rec in records:
            run_id = rec.run_id

            # 1. Campos obrigatórios
            for f in REQUIRED_FIELDS:
                if not getattr(rec, f, None):
                    report.issues.append(QAIssue(
                        run_id=run_id, level="error", code="MISSING_FIELD",
                        message=f"Campo obrigatório ausente: {f}", field=f,
                    ))

            # 2. Métricas obrigatórias
            for m in REQUIRED_METRICS:
                if m not in rec.metrics:
                    report.issues.append(QAIssue(
                        run_id=run_id, level="error", code="MISSING_METRIC",
                        message=f"Métrica obrigatória ausente: {m}", field=m,
                    ))

            # 3. Plausibilidade de métricas
            for metric_name, (lo, hi) in METRIC_BOUNDS.items():
                val = rec.metrics.get(metric_name)
                if val is None:
                    continue
                if not isinstance(val, (int, float)):
                    report.issues.append(QAIssue(
                        run_id=run_id, level="error", code="INVALID_METRIC_TYPE",
                        message=f"{metric_name}={val!r} não é numérico", field=metric_name,
                    ))
                    continue
                if math.isnan(val) or math.isinf(val):
                    report.issues.append(QAIssue(
                        run_id=run_id, level="error", code="IMPLAUSIBLE_METRIC",
                        message=f"{metric_name}={val} — NaN ou Infinito", field=metric_name,
                    ))
                elif not (lo <= val <= hi):
                    report.issues.append(QAIssue(
                        run_id=run_id, level="warning", code="IMPLAUSIBLE_METRIC",
                        message=f"{metric_name}={val:.4f} fora do range [{lo}, {hi}]",
                        field=metric_name,
                    ))

            # 4. Consistência de parâmetros com StrategyRegistry
            if canonical_params:
                for key, canonical_val in canonical_params.items():
                    exp_val = rec.parameters.get(key)
                    if exp_val is None:
                        report.issues.append(QAIssue(
                            run_id=run_id, level="warning", code="PARAM_MISSING",
                            message=f"Parâmetro '{key}' ausente (canônico={canonical_val})",
                            field=key,
                        ))
                    elif type(exp_val) != type(canonical_val):
                        report.issues.append(QAIssue(
                            run_id=run_id, level="warning", code="PARAM_TYPE_MISMATCH",
                            message=f"'{key}': tipo {type(exp_val).__name__} != canônico {type(canonical_val).__name__}",
                            field=key,
                        ))

            # 5. Reproducibility — candles_count e replay_dataset
            if not getattr(rec, "candles_count", None):
                report.issues.append(QAIssue(
                    run_id=run_id, level="warning", code="NO_CANDLES_COUNT",
                    message="candles_count ausente — experimento pode não ser reproduzível",
                ))
            if not getattr(rec, "replay_dataset", None):
                report.issues.append(QAIssue(
                    run_id=run_id, level="info", code="NO_REPLAY_DATASET",
                    message="replay_dataset não especificado",
                ))

            # 6. Duplicatas de run_id
            if run_id in seen_run_ids:
                report.issues.append(QAIssue(
                    run_id=run_id, level="error", code="DUPLICATE_RUN_ID",
                    message=f"run_id duplicado: {run_id}",
                ))
            seen_run_ids.add(run_id)

            # 7. Experimentos idênticos (mesmo params + symbol + tf)
            sig = _build_signature(rec)
            if sig in seen_signatures:
                report.issues.append(QAIssue(
                    run_id=run_id, level="warning", code="DUPLICATE_EXPERIMENT",
                    message=f"Experimento idêntico a run_id={seen_signatures[sig]} (params+symbol+tf)",
                ))
            else:
                seen_signatures[sig] = run_id

        return report


# ── Helpers ───────────────────────────────────────────────────────────────────

def _discover_strategies() -> list[str]:
    """Retorna estratégias com arquivos JSONL em EXPERIMENTS_DIR."""
    if not EXPERIMENTS_DIR.exists():
        return []
    return [
        f.stem
        for f in EXPERIMENTS_DIR.glob("*.jsonl")
        if f.stem != "all_experiments"
    ]


def _build_signature(rec: ExperimentRecord) -> str:
    """Chave de deduplicação: symbol + timeframe + sorted params."""
    import hashlib
    params_str = json.dumps(rec.parameters, sort_keys=True, default=str)
    raw = f"{rec.symbol}:{rec.timeframe}:{params_str}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experiment QA — valida registros do ExperimentTracker")
    parser.add_argument("--strategy", default=None,  help="ID da estratégia (padrão: todas)")
    parser.add_argument("--all",      action="store_true", help="Executar para todas as estratégias")
    parser.add_argument("--json",     action="store_true", help="Saída em JSON")
    args = parser.parse_args()

    qa      = ExperimentQA()
    reports = qa.run(strategy_id=None if args.all else args.strategy)

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        for r in reports:
            print(r.summary())
            print()

"""
parameter_intelligence.py — Phase N FASE 7

Evolutionary Parameter Intelligence.

Analisa padrões de parâmetros nos experimentos históricos para:
  - detectar parâmetros frágeis (performance muito sensível a pequenas mudanças)
  - detectar ranges robustos (performance estável em uma região)
  - detectar ranges perigosos (pico isolado = overfitting de parâmetro)
  - sugerir ranges de exploração para próximos sweeps

IMPORTANTE: Sem RL. Sem neural networks. Heurística quantitativa adaptativa.

Scores produzidos:
  - parameter_stability_score:     estabilidade de um parâmetro (0–100)
  - parameter_range_quality_score: qualidade da região de parâmetros (0–100)
  - adaptive_parameter_priority:   quais parâmetros priorizar no próximo sweep

CLI:
  python -m domains.crypto_coin.research.parameter_intelligence --strategy trend_following
  python -m domains.crypto_coin.research.parameter_intelligence --all --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains.crypto_coin.research.experiment_tracker import ExperimentTracker

EXPERIMENTS_DIR = Path("data/experiments")

# Prometheus (optional)
try:
    from api.metrics import parameter_stability_score as _prom_stability
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ParameterAnalysis:
    """Análise de um parâmetro específico."""
    parameter_name:       str
    values_tested:        list[float | int]
    sharpes_by_value:     dict[str, float]   # str(value) → avg_sharpe

    stability_score:      float   # 0–100 (100 = performance estável nos valores)
    range_quality_score:  float   # 0–100 (100 = região de valores boa)
    is_fragile:           bool    # True se pico isolado ou alta variância
    recommended_range:    tuple[float, float] | None  # min,max robustos
    priority_for_sweep:   float   # 0–100 (100 = altamente prioritário swepar)


@dataclass
class ParameterIntelligenceReport:
    """Relatório de inteligência de parâmetros para uma estratégia."""
    strategy_id:             str
    parameters_analyzed:     list[ParameterAnalysis]
    parameter_stability_score: float   # média composta de todos os parâmetros
    top_fragile_params:      list[str]
    top_stable_params:       list[str]
    sweep_recommendations:   list[str]
    experiments_analyzed:    int
    evaluated_at:            str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["parameters_analyzed"] = [asdict(p) for p in self.parameters_analyzed]
        return d


# ── Analyzer ──────────────────────────────────────────────────────────────────

class ParameterIntelligence:
    """
    FASE 7: Analisa parâmetros de estratégia usando experimentos históricos.

    Método:
      1. Carrega todos os experimentos da estratégia
      2. Agrupa por valor de cada parâmetro
      3. Calcula performance (sharpe) por valor
      4. Detecta estabilidade, fragilidade e ranges robustos
      5. Gera recomendações de sweep adaptativas
    """

    # Thresholds
    FRAGILE_STD_THRESHOLD   = 0.5   # std do sharpe por valor > 0.5 = frágil
    PEAK_ISOLATION_RATIO    = 3.0   # melhor valor / mediana > 3x = pico isolado suspeito
    MIN_VALUES_FOR_ANALYSIS = 2     # mínimo de valores distintos para analisar

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir

    def analyze(self, strategy_id: str) -> ParameterIntelligenceReport:
        """Analisa inteligência de parâmetros de uma estratégia."""
        tracker     = ExperimentTracker(strategy_id=strategy_id, experiments_dir=self.experiments_dir)
        experiments = tracker.load_experiments()

        if not experiments:
            return self._empty_report(strategy_id)

        # Coleta todos os nomes de parâmetros usados
        all_param_names: set[str] = set()
        for exp in experiments:
            all_param_names.update(exp.parameters.keys())

        # Remove parâmetros que são constantes ou strings
        numeric_params = {
            name for name in all_param_names
            if all(isinstance(e.parameters.get(name), (int, float)) for e in experiments
                   if name in e.parameters)
        }

        analyzed: list[ParameterAnalysis] = []
        for param_name in sorted(numeric_params):
            pa = self._analyze_parameter(param_name, experiments)
            if pa is not None:
                analyzed.append(pa)

        if not analyzed:
            return self._empty_report(strategy_id, experiments_count=len(experiments))

        # Composta
        avg_stability = statistics.mean(a.stability_score for a in analyzed)
        fragile_params = [a.parameter_name for a in analyzed if a.is_fragile]
        stable_params  = sorted(
            [a.parameter_name for a in analyzed if not a.is_fragile and a.stability_score >= 70],
            key=lambda p: next(a.stability_score for a in analyzed if a.parameter_name == p),
            reverse=True,
        )

        # Recomendações de sweep
        recs = self._generate_sweep_recommendations(analyzed)

        # Emite métrica
        if _METRICS_AVAILABLE:
            try:
                _prom_stability.set(avg_stability)
            except Exception:
                pass

        return ParameterIntelligenceReport(
            strategy_id               = strategy_id,
            parameters_analyzed       = analyzed,
            parameter_stability_score = round(avg_stability, 1),
            top_fragile_params        = fragile_params[:5],
            top_stable_params         = stable_params[:5],
            sweep_recommendations     = recs,
            experiments_analyzed      = len(experiments),
            evaluated_at              = datetime.now(timezone.utc).isoformat(),
        )

    def _analyze_parameter(
        self, param_name: str, experiments: list[Any]
    ) -> ParameterAnalysis | None:
        """Analisa um único parâmetro numérico."""
        # Agrupa experimentos por valor do parâmetro
        value_to_sharpes: dict[float, list[float]] = {}
        for exp in experiments:
            if param_name not in exp.parameters:
                continue
            val   = float(exp.parameters[param_name])
            sharpe = exp.metrics.get("sharpe", 0.0)
            value_to_sharpes.setdefault(val, []).append(sharpe)

        if len(value_to_sharpes) < self.MIN_VALUES_FOR_ANALYSIS:
            return None

        values_tested = sorted(value_to_sharpes.keys())
        sharpes_by_value: dict[str, float] = {
            str(v): round(statistics.mean(s), 3) for v, s in value_to_sharpes.items()
        }

        avg_sharpes = list(sharpes_by_value.values())

        # ── Stability score ────────────────────────────────────────────────────
        try:
            std_of_avgs = statistics.stdev(avg_sharpes)
        except statistics.StatisticsError:
            std_of_avgs = 0.0
        # Low std → high stability
        stability_score = max(0.0, 100.0 - (std_of_avgs / self.FRAGILE_STD_THRESHOLD) * 60.0)
        stability_score = min(100.0, stability_score)

        # ── Peak isolation (overfitting de parâmetro) ──────────────────────────
        best_avg   = max(avg_sharpes)
        median_avg = statistics.median(avg_sharpes)
        is_peak    = (
            median_avg > 0 and best_avg / median_avg > self.PEAK_ISOLATION_RATIO
        )
        is_fragile = std_of_avgs > self.FRAGILE_STD_THRESHOLD or is_peak

        # ── Range quality ──────────────────────────────────────────────────────
        # Boa qualidade = maioria dos valores tem sharpe positivo
        positive_values = sum(1 for s in avg_sharpes if s > 0.3)
        range_quality   = (positive_values / len(avg_sharpes)) * 100.0

        # ── Recommended range ──────────────────────────────────────────────────
        recommended_range: tuple[float, float] | None = None
        robust_values = [v for v, s in zip(values_tested, avg_sharpes) if s > 0.3]
        if len(robust_values) >= 2:
            recommended_range = (min(robust_values), max(robust_values))

        # ── Priority for sweep ─────────────────────────────────────────────────
        priority = (
            (std_of_avgs / self.FRAGILE_STD_THRESHOLD) * 40.0
            + (20.0 if is_peak else 0.0)
            + max(0.0, 30.0 - range_quality * 0.3)
        )
        priority = min(100.0, priority)

        return ParameterAnalysis(
            parameter_name      = param_name,
            values_tested       = values_tested,
            sharpes_by_value    = sharpes_by_value,
            stability_score     = round(stability_score, 1),
            range_quality_score = round(range_quality, 1),
            is_fragile          = is_fragile,
            recommended_range   = recommended_range,
            priority_for_sweep  = round(priority, 1),
        )

    def _generate_sweep_recommendations(
        self, analyzed: list[ParameterAnalysis]
    ) -> list[str]:
        """Gera recomendações de sweep adaptativas."""
        recs: list[str] = []
        # Ordena por prioridade de sweep
        priority_sorted = sorted(analyzed, key=lambda a: a.priority_for_sweep, reverse=True)
        for pa in priority_sorted[:3]:
            if pa.is_fragile and pa.recommended_range:
                lo, hi = pa.recommended_range
                mid    = (lo + hi) / 2.0
                step   = max(0.1, (hi - lo) / 10.0)
                recs.append(
                    f"Sweep '{pa.parameter_name}' em range robusto [{lo:.1f}–{hi:.1f}], "
                    f"center={mid:.1f}, step≈{step:.2f} — parâmetro frágil"
                )
            elif pa.is_fragile:
                recs.append(
                    f"Explorar '{pa.parameter_name}' em range mais amplo — "
                    f"performance muito concentrada (possível pico de overfitting)"
                )
            elif pa.range_quality_score < 50:
                recs.append(
                    f"Revisar '{pa.parameter_name}' — menos de 50% dos valores testados são positivos"
                )
        return recs

    def _empty_report(
        self, strategy_id: str, experiments_count: int = 0
    ) -> ParameterIntelligenceReport:
        return ParameterIntelligenceReport(
            strategy_id=strategy_id,
            parameters_analyzed=[],
            parameter_stability_score=50.0,
            top_fragile_params=[],
            top_stable_params=[],
            sweep_recommendations=["Execute sweep_runner para gerar dados de parâmetros."],
            experiments_analyzed=experiments_count,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )


# ── Fleet ─────────────────────────────────────────────────────────────────────

class ParameterIntelligenceFleet:
    """Analisa parâmetros de toda a frota e identifica padrões globais."""

    def __init__(self, experiments_dir: Path = EXPERIMENTS_DIR):
        self.experiments_dir = experiments_dir
        self.engine          = ParameterIntelligence(experiments_dir)

    def analyze_all(self) -> list[ParameterIntelligenceReport]:
        strategy_files = list(self.experiments_dir.glob("*.jsonl"))
        strategy_ids   = [f.stem for f in strategy_files if f.stem != "all_experiments"]
        reports = []
        for sid in strategy_ids:
            try:
                reports.append(self.engine.analyze(sid))
            except Exception as e:
                print(f"[WARN] Erro ao analisar {sid}: {e}")
        return sorted(reports, key=lambda r: r.parameter_stability_score)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parameter Intelligence — Phase N FASE 7"
    )
    parser.add_argument("--strategy", help="Estratégia específica")
    parser.add_argument("--all",  action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.strategy:
        engine = ParameterIntelligence()
        report = engine.analyze(args.strategy)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"\nParameter Intelligence — {report.strategy_id}")
            print(f"  parameter_stability_score: {report.parameter_stability_score:.0f}/100")
            print(f"  experiments_analyzed:      {report.experiments_analyzed}")
            print(f"  fragile_params:            {report.top_fragile_params}")
            print(f"  stable_params:             {report.top_stable_params}")
            print("\n  Parâmetros analisados:")
            for pa in sorted(report.parameters_analyzed, key=lambda a: a.priority_for_sweep, reverse=True):
                fragile_marker = " ⚠️" if pa.is_fragile else ""
                print(
                    f"    {pa.parameter_name:<30} stability={pa.stability_score:.0f} "
                    f"quality={pa.range_quality_score:.0f} sweep_priority={pa.priority_for_sweep:.0f}{fragile_marker}"
                )
            if report.sweep_recommendations:
                print("\n  Recomendações de sweep:")
                for rec in report.sweep_recommendations:
                    print(f"    → {rec}")

    elif args.all:
        fleet  = ParameterIntelligenceFleet()
        reports = fleet.analyze_all()
        if args.json:
            print(json.dumps([r.to_dict() for r in reports], indent=2))
        else:
            print(f"\nParameter Intelligence Fleet ({len(reports)} estratégias)")
            print(f"{'Estratégia':<25} {'Stability':>10} {'Frágeis':>8} {'Params':>7}")
            print("-" * 55)
            for r in reports:
                print(
                    f"{r.strategy_id:<25} {r.parameter_stability_score:>10.0f} "
                    f"{len(r.top_fragile_params):>8} {len(r.parameters_analyzed):>7}"
                )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

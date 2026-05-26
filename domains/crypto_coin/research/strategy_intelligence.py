"""
strategy_intelligence.py — Phase L FASE 9

Inteligência de análise de estratégias quantitativas.

Detecta padrões problemáticos nos experimentos registrados:
  - Degradação temporal (performance declining over time)
  - Instabilidade de regime (performance inconsistente por regime de mercado)
  - Indicadores de overfitting (curvas muito suaves, consistência artificial)
  - Fragilidade de parâmetros (resultado muito sensível a pequenas variações)
  - Consistency score (0–100: quanto mais alto, mais confiável a estratégia)

Entrada: ExperimentTracker (JSONL de experimentos anteriores)
Saída:   StrategyIntelligenceReport por estratégia

CLI:
  python -m domains.crypto_coin.research.strategy_intelligence --strategy trend_following
  python -m domains.crypto_coin.research.strategy_intelligence --all
  python -m domains.crypto_coin.research.strategy_intelligence --alert  # só estratégias com problemas
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from typing import Any


# ── Interfaces ────────────────────────────────────────────────────────────────

@dataclass
class DegradationSignal:
    """Sinal de degradação temporal de performance."""
    detected:      bool
    severity:      str   = "none"  # none | low | medium | high
    recent_sharpe: float = 0.0
    prior_sharpe:  float = 0.0
    drop_pct:      float = 0.0
    message:       str   = ""

@dataclass
class OverfitSignal:
    """Indicadores de overfitting."""
    suspected:         bool
    train_test_gap:    float = 0.0   # diferença entre in-sample e out-of-sample
    consistency_too_high: bool = False  # sharpe > 3.5 em todos os runs = suspeito
    parameter_count:   int   = 0
    message:           str   = ""

@dataclass
class ParameterFragilitySignal:
    """Fragilidade de parâmetros — pequena variação causa grande impacto."""
    fragile:          bool
    sharpe_std:       float = 0.0  # desvio padrão de sharpe entre runs do sweep
    best_worst_ratio: float = 0.0  # sharpe do melhor / sharpe do pior
    message:          str   = ""

@dataclass
class RegimeInstabilitySignal:
    """Instabilidade entre regimes de mercado."""
    unstable:     bool
    best_regime:  str   = ""
    worst_regime: str   = ""
    regime_gap:   float = 0.0   # diferença de sharpe entre melhor e pior regime
    message:      str   = ""

@dataclass
class StrategyIntelligenceReport:
    strategy_id:       str
    consistency_score: float   # 0–100
    experiments_count: int
    degradation:       DegradationSignal
    overfit:           OverfitSignal
    fragility:         ParameterFragilitySignal
    regime_instability: RegimeInstabilitySignal
    recommendation:    str
    evaluated_at:      str

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

    def has_alerts(self) -> bool:
        return (
            self.degradation.detected
            or self.overfit.suspected
            or self.fragility.fragile
            or self.regime_instability.unstable
        )


# ── Analyzer ─────────────────────────────────────────────────────────────────

class StrategyIntelligenceAnalyzer:
    """
    Analisa a inteligência de uma estratégia a partir de seus experimentos.

    Lê experimentos do ExperimentTracker (JSONL) e detecta sinais problemáticos.
    """

    def analyze(
        self,
        strategy_id: str,
        symbol:      str | None = None,
        timeframe:   str | None = None,
    ) -> StrategyIntelligenceReport:
        from .experiment_tracker import ExperimentTracker
        from datetime import datetime, timezone

        tracker = ExperimentTracker()
        filters: dict[str, Any] = {"strategy_id": strategy_id}
        if symbol:    filters["symbol"]    = symbol
        if timeframe: filters["timeframe"] = timeframe

        experiments = tracker.load_all(**filters)

        if not experiments:
            return StrategyIntelligenceReport(
                strategy_id        = strategy_id,
                consistency_score  = 0.0,
                experiments_count  = 0,
                degradation        = DegradationSignal(detected=False, message="Sem dados suficientes"),
                overfit            = OverfitSignal(suspected=False, message="Sem dados suficientes"),
                fragility          = ParameterFragilitySignal(fragile=False, message="Sem dados suficientes"),
                regime_instability = RegimeInstabilitySignal(unstable=False, message="Sem dados suficientes"),
                recommendation     = "Executar pelo menos 5 experimentos antes de analisar.",
                evaluated_at       = datetime.now(timezone.utc).isoformat(),
            )

        sharpe_values = [
            e.metrics.get("sharpe", 0.0) for e in experiments
            if e.metrics.get("sharpe") is not None
        ]

        degradation        = self._detect_degradation(experiments, sharpe_values)
        overfit            = self._detect_overfit(experiments, sharpe_values)
        fragility          = self._detect_parameter_fragility(experiments, sharpe_values)
        regime_instability = self._detect_regime_instability(experiments)
        consistency_score  = self._compute_consistency_score(
            experiments, sharpe_values, degradation, overfit, fragility, regime_instability
        )
        recommendation     = self._build_recommendation(
            consistency_score, degradation, overfit, fragility, regime_instability
        )

        # Prometheus wiring
        self._emit_metrics(strategy_id, consistency_score, degradation)

        return StrategyIntelligenceReport(
            strategy_id        = strategy_id,
            consistency_score  = round(consistency_score, 1),
            experiments_count  = len(experiments),
            degradation        = degradation,
            overfit            = overfit,
            fragility          = fragility,
            regime_instability = regime_instability,
            recommendation     = recommendation,
            evaluated_at       = datetime.now(timezone.utc).isoformat(),
        )

    # ── Degradação temporal ───────────────────────────────────────────────────

    def _detect_degradation(self, experiments: list, sharpe_values: list[float]) -> DegradationSignal:
        if len(experiments) < 4:
            return DegradationSignal(detected=False, message="Mínimo 4 experimentos para detectar degradação")

        # Ordenar por data de criação
        sorted_exps = sorted(experiments, key=lambda e: e.created_at)
        n = len(sorted_exps)
        mid = n // 2

        recent_sharpes = [e.metrics.get("sharpe", 0) for e in sorted_exps[mid:] if e.metrics.get("sharpe") is not None]
        prior_sharpes  = [e.metrics.get("sharpe", 0) for e in sorted_exps[:mid]  if e.metrics.get("sharpe") is not None]

        if not recent_sharpes or not prior_sharpes:
            return DegradationSignal(detected=False)

        recent_avg = sum(recent_sharpes) / len(recent_sharpes)
        prior_avg  = sum(prior_sharpes)  / len(prior_sharpes)

        if prior_avg <= 0:
            return DegradationSignal(detected=False, message="Baseline sem performance positiva")

        drop_pct = ((prior_avg - recent_avg) / abs(prior_avg)) * 100

        if drop_pct > 30:
            return DegradationSignal(
                detected=True,
                severity="high",
                recent_sharpe=round(recent_avg, 2),
                prior_sharpe=round(prior_avg, 2),
                drop_pct=round(drop_pct, 1),
                message=f"Sharpe caiu {drop_pct:.1f}% (de {prior_avg:.2f} para {recent_avg:.2f})"
            )
        elif drop_pct > 15:
            return DegradationSignal(
                detected=True,
                severity="medium",
                recent_sharpe=round(recent_avg, 2),
                prior_sharpe=round(prior_avg, 2),
                drop_pct=round(drop_pct, 1),
                message=f"Sharpe em queda moderada: {drop_pct:.1f}%"
            )

        return DegradationSignal(
            detected=False,
            recent_sharpe=round(recent_avg, 2),
            prior_sharpe=round(prior_avg, 2),
        )

    # ── Overfitting ───────────────────────────────────────────────────────────

    def _detect_overfit(self, experiments: list, sharpe_values: list[float]) -> OverfitSignal:
        if len(sharpe_values) < 3:
            return OverfitSignal(suspected=False, message="Dados insuficientes")

        # Sinal 1: Sharpe muito alto e muito consistente (> 3.5 em todos) = suspeito
        consistency_too_high = all(s > 3.5 for s in sharpe_values) and len(sharpe_values) >= 3

        # Sinal 2: contagem de parâmetros (heurística — mais parâmetros = mais risco de overfit)
        param_counts = [len(e.parameters) for e in experiments if e.parameters]
        avg_params   = sum(param_counts) / len(param_counts) if param_counts else 0

        # Sinal 3: sem variação de drawdown (drawdown estável + sharpe alto = suspeito)
        drawdowns = [abs(e.metrics.get("max_drawdown", 0)) for e in experiments]
        dd_std    = _std(drawdowns)

        suspected = consistency_too_high or (avg_params > 8 and sum(sharpe_values) / len(sharpe_values) > 2.0)

        return OverfitSignal(
            suspected             = suspected,
            consistency_too_high  = consistency_too_high,
            parameter_count       = round(avg_params),
            message = "Sharpe muito alto e consistente — validar em dados out-of-sample" if suspected else "",
        )

    # ── Fragilidade de parâmetros ─────────────────────────────────────────────

    def _detect_parameter_fragility(self, experiments: list, sharpe_values: list[float]) -> ParameterFragilitySignal:
        # Pegar apenas experimentos de sweep (tags contém "sweep")
        sweep_exps = [e for e in experiments if "sweep" in (e.tags or [])]

        if len(sweep_exps) < 3:
            return ParameterFragilitySignal(fragile=False, message="Poucos dados de sweep para análise")

        sweep_sharpes = [e.metrics.get("sharpe", 0) for e in sweep_exps if e.metrics.get("sharpe") is not None]

        if len(sweep_sharpes) < 2:
            return ParameterFragilitySignal(fragile=False)

        sharpe_std = _std(sweep_sharpes)
        best  = max(sweep_sharpes)
        worst = min(sweep_sharpes)

        # Fragilidade: desvio padrão alto OU razão best/worst extrema
        ratio   = best / abs(worst) if worst != 0 else float("inf")
        fragile = sharpe_std > 0.8 or ratio > 5.0

        return ParameterFragilitySignal(
            fragile          = fragile,
            sharpe_std       = round(sharpe_std, 3),
            best_worst_ratio = round(ratio, 2),
            message = f"Alta variância entre configurações (std={sharpe_std:.2f}) — parâmetros frágeis" if fragile else "",
        )

    # ── Instabilidade de regime ───────────────────────────────────────────────

    def _detect_regime_instability(self, experiments: list) -> RegimeInstabilitySignal:
        # Coletar performance por regime (de regime_performance field)
        regime_sharpes: dict[str, list[float]] = {}

        for e in experiments:
            rp = e.regime_performance or {}
            for regime, data in rp.items():
                if isinstance(data, dict) and "sharpe" in data:
                    regime_sharpes.setdefault(regime, []).append(data["sharpe"])

        if len(regime_sharpes) < 2:
            return RegimeInstabilitySignal(unstable=False, message="Dados de regime insuficientes")

        avg_per_regime = {
            r: sum(vals) / len(vals) for r, vals in regime_sharpes.items()
        }

        best_regime  = max(avg_per_regime, key=lambda r: avg_per_regime[r])
        worst_regime = min(avg_per_regime, key=lambda r: avg_per_regime[r])
        gap          = avg_per_regime[best_regime] - avg_per_regime[worst_regime]

        unstable = gap > 2.0  # diferença > 2 sharpe units = instabilidade

        return RegimeInstabilitySignal(
            unstable     = unstable,
            best_regime  = best_regime,
            worst_regime = worst_regime,
            regime_gap   = round(gap, 2),
            message = f"Gap de {gap:.1f} sharpe entre {best_regime} e {worst_regime}" if unstable else "",
        )

    # ── Consistency score ─────────────────────────────────────────────────────

    def _compute_consistency_score(
        self,
        experiments: list,
        sharpe_values: list[float],
        degradation: DegradationSignal,
        overfit: OverfitSignal,
        fragility: ParameterFragilitySignal,
        regime: RegimeInstabilitySignal,
    ) -> float:
        if not sharpe_values:
            return 0.0

        # Base: % de experimentos com sharpe > 0
        positive_pct = sum(1 for s in sharpe_values if s > 0) / len(sharpe_values) * 100

        # Penalidades
        penalty = 0.0
        if degradation.detected:
            penalty += 20 if degradation.severity == "high" else 10
        if overfit.suspected:
            penalty += 15
        if fragility.fragile:
            penalty += 10
        if regime.unstable:
            penalty += 10

        # Bônus por volume de experimentos
        exp_bonus = min(10, len(experiments) * 2)

        score = max(0.0, min(100.0, positive_pct - penalty + exp_bonus))
        return round(score, 1)

    # ── Recomendação ──────────────────────────────────────────────────────────

    def _emit_metrics(
        self,
        strategy_id:      str,
        consistency_score: float,
        degradation:       DegradationSignal,
    ) -> None:
        try:
            from api import metrics as prom
            prom.strategy_consistency_score.labels(strategy_id=strategy_id).set(consistency_score)
            if degradation.detected:
                prom.strategy_degradation_total.labels(
                    strategy_id=strategy_id,
                    severity=degradation.severity,
                ).inc()
        except Exception:
            pass

    def _build_recommendation(
        self,
        consistency_score: float,
        degradation: DegradationSignal,
        overfit: OverfitSignal,
        fragility: ParameterFragilitySignal,
        regime: RegimeInstabilitySignal,
    ) -> str:
        if consistency_score >= 80:
            return "✅ Estratégia consistente — candidata a portfólio de produção."
        if consistency_score >= 60:
            recs = []
            if degradation.detected:  recs.append("monitorar degradação")
            if regime.unstable:       recs.append("evitar regimes desfavoráveis")
            return f"🟡 Performance moderada — {'; '.join(recs) or 'continuar monitorando'}."
        recs = []
        if overfit.suspected:   recs.append("validar out-of-sample")
        if fragility.fragile:   recs.append("robustecer parâmetros")
        if degradation.detected: recs.append("investigar causa de degradação")
        return f"🔴 Performance baixa — ação recomendada: {'; '.join(recs) or 'revisar estratégia'}."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy Intelligence Analyzer")
    parser.add_argument("--strategy", help="ID da estratégia a analisar")
    parser.add_argument("--all",      action="store_true", help="Analisar todas as estratégias do registry")
    parser.add_argument("--alert",    action="store_true", help="Mostrar apenas estratégias com alertas")
    parser.add_argument("--symbol",   default=None)
    parser.add_argument("--tf",       default=None)
    parser.add_argument("--json",     action="store_true")
    args = parser.parse_args()

    analyzer = StrategyIntelligenceAnalyzer()

    strategies: list[str] = []
    if args.strategy:
        strategies = [args.strategy]
    elif args.all:
        try:
            from .strategy_registry import get_registry
            strategies = list(get_registry().list_strategies().keys())
        except Exception as e:
            print(f"Erro ao carregar registry: {e}")
            return
    else:
        parser.print_help()
        return

    reports = [
        analyzer.analyze(sid, symbol=args.symbol, timeframe=args.tf)
        for sid in strategies
    ]

    if args.alert:
        reports = [r for r in reports if r.has_alerts()]

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
        return

    for report in reports:
        print(f"\n{'─'*55}")
        print(f"Strategy: {report.strategy_id}")
        print(f"Consistency Score: {report.consistency_score:.1f}/100")
        print(f"Experiments: {report.experiments_count}")
        if report.degradation.detected:
            print(f"⚠️  Degradação: {report.degradation.message}")
        if report.overfit.suspected:
            print(f"⚠️  Overfit suspeito: {report.overfit.message}")
        if report.fragility.fragile:
            print(f"⚠️  Parâmetros frágeis: {report.fragility.message}")
        if report.regime_instability.unstable:
            print(f"⚠️  Instabilidade de regime: {report.regime_instability.message}")
        print(f"📋 {report.recommendation}")
    print()


if __name__ == "__main__":
    main()

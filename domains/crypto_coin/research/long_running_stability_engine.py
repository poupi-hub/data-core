"""
long_running_stability_engine.py — Phase R R-4

Long-Running Stability Monitor.

Validates continuous runtime stability for long-running sessions,
detecting decay and drift across multiple operational dimensions.

Scores produzidos:
  - runtime_health_score:          saude geral do runtime (0-100)
  - operational_decay_score:       nivel de decaimento operacional (0-100, 100=pior)
  - long_running_stability_score:  estabilidade de longa duracao (0-100)
  - runtime_consistency_score:     consistencia entre dimensoes (0-100)

Dimensoes avaliadas (7 total):
  1. memory_growth:       crescimento de arquivos data/
  2. loop_consistency:    variancia do live_governance_score
  3. scheduler_timing:    consistencia dos intervalos no governance_history
  4. replay_fidelity:     score de fidelidade do replay mais recente
  5. metrics_continuity:  atualidade do live_governance_summary
  6. governance_stability: variancia do governance_health_score
  7. readiness_drift:     tendencia do continuous_live_readiness_score

stability_status:
  STABLE    >= 75
  DRIFTING  >= 55
  DEGRADING >= 35
  CRITICAL  <  35

CLI:
  python -m domains.crypto_coin.research.long_running_stability_engine
  python -m domains.crypto_coin.research.long_running_stability_engine --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STABILITY_LOG = Path("data/stability_log.jsonl")

# Source logs (read-only)
GOV_SUMMARY_LOG     = Path("data/live_governance_summary.jsonl")
GOVERNANCE_HIST_LOG = Path("data/governance_history.jsonl")
REPLAY_LOG          = Path("data/live_execution_replay_log.jsonl")
REVALID_LOG         = Path("data/live_readiness_revalidation_log.jsonl")
RUNTIME_STATE       = Path("data/runtime_state.json")
DATA_DIR            = Path("data")

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        runtime_health_score        as _prom_runtime_health,
        operational_decay_score     as _prom_decay,
        long_running_stability_score as _prom_stability,
        runtime_consistency_score   as _prom_consistency,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Thresholds ──────────────────────────────────────────────────────────────────

SCORE_STABLE    = 75.0
SCORE_DRIFTING  = 55.0
SCORE_DEGRADING = 35.0

DECAY_THRESHOLD = 50.0   # dimension score below this → decay detected


# ── Data classes ────────────────────────────────────────────────────────────────

@dataclass
class StabilityDimension:
    name:   str
    score:  float   # 0-100
    trend:  str     # stable | improving | degrading
    detail: str


@dataclass
class StabilityReport:
    """Relatorio de estabilidade de longa duracao."""
    report_id:                    str
    runtime_health_score:         float   # 0-100
    operational_decay_score:      float   # 0-100 (100 = pior)
    long_running_stability_score: float   # 0-100
    runtime_consistency_score:    float   # 0-100
    dimensions:                   list[StabilityDimension]
    overall_trend:                str     # stable | improving | degrading
    session_age_hours:            float
    decay_detected:               bool
    decay_sources:                list[str]
    stability_status:             str     # STABLE | DRIFTING | DEGRADING | CRITICAL
    recommendation:               str
    evaluated_at:                 str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dimensions"] = [asdict(dim) for dim in self.dimensions]
        return d


# ── Engine ──────────────────────────────────────────────────────────────────────

class LongRunningStabilityEngine:
    """
    R-4: Monitor de estabilidade de longa duracao.

    Avalia 7 dimensoes operacionais para detectar decaimento e drift
    em sessoes de execucao prolongada.
    """

    def __init__(self, stability_log: Path = STABILITY_LOG):
        self.stability_log = stability_log

    def evaluate(self) -> StabilityReport:
        """Avalia estabilidade do runtime e retorna relatorio."""
        report_id = str(uuid.uuid4())[:10]

        dimensions = self._evaluate_all_dimensions()

        scores = [d.score for d in dimensions]
        long_running_stability_score = round(sum(scores) / len(scores), 1)

        operational_decay_score   = round(100.0 - long_running_stability_score, 1)
        runtime_health_score      = round(min(long_running_stability_score * 1.1, 100.0), 1)

        variance = self._variance(scores)
        runtime_consistency_score = (
            100.0 if variance < 15.0
            else round(max(0.0, 100.0 - variance), 1)
        )

        decay_detected = any(d.score < DECAY_THRESHOLD for d in dimensions)
        decay_sources  = [d.name for d in dimensions if d.score < DECAY_THRESHOLD]

        stability_status = (
            "STABLE"    if long_running_stability_score >= SCORE_STABLE    else
            "DRIFTING"  if long_running_stability_score >= SCORE_DRIFTING  else
            "DEGRADING" if long_running_stability_score >= SCORE_DEGRADING else
            "CRITICAL"
        )

        session_age_hours = self._compute_session_age()
        overall_trend     = self._compute_overall_trend()
        recommendation    = self._build_recommendation(
            long_running_stability_score, stability_status,
            decay_detected, decay_sources, overall_trend,
        )

        report = StabilityReport(
            report_id                    = report_id,
            runtime_health_score         = runtime_health_score,
            operational_decay_score      = operational_decay_score,
            long_running_stability_score = long_running_stability_score,
            runtime_consistency_score    = runtime_consistency_score,
            dimensions                   = dimensions,
            overall_trend                = overall_trend,
            session_age_hours            = round(session_age_hours, 2),
            decay_detected               = decay_detected,
            decay_sources                = decay_sources,
            stability_status             = stability_status,
            recommendation               = recommendation,
            evaluated_at                 = datetime.now(timezone.utc).isoformat(),
        )

        self._persist(report)
        self._push_metrics(report)
        return report

    # ── Dimension evaluators ────────────────────────────────────────────────────

    def _evaluate_all_dimensions(self) -> list[StabilityDimension]:
        return [
            self._dim_memory_growth(),
            self._dim_loop_consistency(),
            self._dim_scheduler_timing(),
            self._dim_replay_fidelity(),
            self._dim_metrics_continuity(),
            self._dim_governance_stability(),
            self._dim_readiness_drift(),
        ]

    def _dim_memory_growth(self) -> StabilityDimension:
        """Verifica crescimento excessivo dos arquivos data/."""
        try:
            if not DATA_DIR.exists():
                return StabilityDimension(
                    name="memory_growth", score=80.0,
                    trend="stable", detail="data/ nao encontrado — estimativa padrao",
                )
            total_bytes = sum(
                f.stat().st_size
                for f in DATA_DIR.rglob("*")
                if f.is_file()
            )
            total_mb = total_bytes / (1024 * 1024)
            # < 100 MB = saudavel, > 500 MB = preocupante
            if total_mb < 100:
                score, trend, detail = 90.0, "stable", f"data/ size={total_mb:.1f}MB (saudavel)"
            elif total_mb < 250:
                score, trend, detail = 75.0, "stable", f"data/ size={total_mb:.1f}MB (moderado)"
            elif total_mb < 500:
                score, trend, detail = 55.0, "degrading", f"data/ size={total_mb:.1f}MB (elevado)"
            else:
                score, trend, detail = 30.0, "degrading", f"data/ size={total_mb:.1f}MB (critico)"
        except Exception as exc:
            score, trend, detail = 80.0, "stable", f"nao mensuravel ({exc}) — estimativa padrao"

        return StabilityDimension(name="memory_growth", score=score, trend=trend, detail=detail)

    def _dim_loop_consistency(self) -> StabilityDimension:
        """Verifica variancia do live_governance_score nos ultimos 5 ciclos."""
        records = self._load_log(GOV_SUMMARY_LOG, n=5)
        if len(records) < 2:
            return StabilityDimension(
                name="loop_consistency", score=75.0, trend="stable",
                detail=f"poucos registros ({len(records)}) — estimativa padrao",
            )
        scores_list = [r.get("live_governance_score", 75.0) for r in records]
        spread = max(scores_list) - min(scores_list)
        if spread < 10:
            score, trend, detail = 90.0, "stable", f"variancia={spread:.1f} pts (excelente)"
        elif spread < 20:
            score, trend, detail = 70.0, "stable", f"variancia={spread:.1f} pts (aceitavel)"
        else:
            score, trend, detail = 50.0, "degrading", f"variancia={spread:.1f} pts (alta)"
        return StabilityDimension(name="loop_consistency", score=score, trend=trend, detail=detail)

    def _dim_scheduler_timing(self) -> StabilityDimension:
        """Verifica consistencia dos intervalos no governance_history."""
        records = self._load_log(GOVERNANCE_HIST_LOG, n=3)
        if len(records) < 2:
            return StabilityDimension(
                name="scheduler_timing", score=75.0, trend="stable",
                detail=f"poucos registros ({len(records)}) — estimativa padrao",
            )
        timestamps: list[float] = []
        for r in records:
            ts_str = r.get("evaluated_at") or r.get("timestamp")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    timestamps.append(dt.timestamp())
                except Exception:
                    pass
        if len(timestamps) < 2:
            return StabilityDimension(
                name="scheduler_timing", score=75.0, trend="stable",
                detail="timestamps invalidos — estimativa padrao",
            )
        intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps) - 1)]
        avg_interval = sum(intervals) / len(intervals)
        max_variance_min = max(abs(iv - avg_interval) for iv in intervals) / 60.0
        if max_variance_min < 5:
            score, trend = 90.0, "stable"
            detail = f"intervalos consistentes (var={max_variance_min:.1f} min)"
        else:
            score, trend = 60.0, "degrading"
            detail = f"intervalos inconsistentes (var={max_variance_min:.1f} min)"
        return StabilityDimension(name="scheduler_timing", score=score, trend=trend, detail=detail)

    def _dim_replay_fidelity(self) -> StabilityDimension:
        """Le avg_fidelity_score do ultimo registro do replay log."""
        records = self._load_log(REPLAY_LOG, n=1)
        if not records:
            return StabilityDimension(
                name="replay_fidelity", score=75.0, trend="stable",
                detail="replay log nao encontrado — estimativa padrao 75",
            )
        raw = records[-1].get("avg_fidelity_score", 0.75)
        # pode estar em escala 0-1 ou 0-100
        fidelity = float(raw)
        if fidelity <= 1.0:
            fidelity *= 100.0
        fidelity = max(0.0, min(100.0, fidelity))
        trend = "stable" if fidelity >= 70 else "degrading"
        detail = f"avg_fidelity_score={fidelity:.1f}/100"
        return StabilityDimension(name="replay_fidelity", score=fidelity, trend=trend, detail=detail)

    def _dim_metrics_continuity(self) -> StabilityDimension:
        """Verifica se live_governance_summary foi atualizado recentemente."""
        if not GOV_SUMMARY_LOG.exists():
            return StabilityDimension(
                name="metrics_continuity", score=30.0, trend="degrading",
                detail="live_governance_summary.jsonl nao encontrado",
            )
        try:
            mtime = GOV_SUMMARY_LOG.stat().st_mtime
            now   = datetime.now(timezone.utc).timestamp()
            age_min = (now - mtime) / 60.0
            if age_min <= 60:
                score, trend = 90.0, "stable"
                detail = f"atualizado ha {age_min:.0f} min (recente)"
            elif age_min <= 120:
                score, trend = 60.0, "stable"
                detail = f"atualizado ha {age_min:.0f} min (moderado)"
            else:
                score, trend = 30.0, "degrading"
                detail = f"atualizado ha {age_min:.0f} min (desatualizado)"
        except Exception as exc:
            score, trend, detail = 75.0, "stable", f"erro ao verificar mtime: {exc}"
        return StabilityDimension(name="metrics_continuity", score=score, trend=trend, detail=detail)

    def _dim_governance_stability(self) -> StabilityDimension:
        """Verifica variancia do governance_health_score nos ultimos 5 registros."""
        records = self._load_log(GOVERNANCE_HIST_LOG, n=5)
        if len(records) < 2:
            return StabilityDimension(
                name="governance_stability", score=75.0, trend="stable",
                detail=f"poucos registros ({len(records)}) — estimativa padrao",
            )
        scores_list = [float(r.get("governance_health_score", 75.0)) for r in records]
        var = self._variance(scores_list)
        if var < 5:
            score, trend, detail = 95.0, "stable", f"variancia={var:.1f} (excelente)"
        elif var < 15:
            score, trend, detail = 75.0, "stable", f"variancia={var:.1f} (aceitavel)"
        else:
            score, trend, detail = 50.0, "degrading", f"variancia={var:.1f} (alta)"
        return StabilityDimension(name="governance_stability", score=score, trend=trend, detail=detail)

    def _dim_readiness_drift(self) -> StabilityDimension:
        """Avalia tendencia do continuous_live_readiness_score."""
        records = self._load_log(REVALID_LOG, n=3)
        if len(records) < 2:
            return StabilityDimension(
                name="readiness_drift", score=75.0, trend="stable",
                detail=f"poucos registros ({len(records)}) — estimativa padrao",
            )
        scores_list = [float(r.get("continuous_live_readiness_score", 75.0)) for r in records]
        latest = scores_list[-1]
        delta  = scores_list[-1] - scores_list[0]

        if abs(delta) < 3.0:
            trend = "stable"
        elif delta > 0:
            trend = "improving"
        else:
            trend = "degrading"

        # Score baseado no valor mais recente
        score  = max(0.0, min(100.0, latest))
        detail = (
            f"readiness={latest:.1f} delta={delta:+.1f} "
            f"em {len(records)} registros ({trend})"
        )
        return StabilityDimension(name="readiness_drift", score=score, trend=trend, detail=detail)

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _variance(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / len(values)

    def _compute_session_age(self) -> float:
        """Le session_start de data/runtime_state.json e computa idade em horas."""
        try:
            if not RUNTIME_STATE.exists():
                return 0.0
            with open(RUNTIME_STATE) as f:
                state = json.load(f)
            start_str = state.get("session_start")
            if not start_str:
                return 0.0
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            now_dt   = datetime.now(timezone.utc)
            return (now_dt - start_dt).total_seconds() / 3600.0
        except Exception:
            return 0.0

    def _compute_overall_trend(self) -> str:
        """Calcula tendencia geral com base nos ultimos 5 registros do stability_log."""
        records = self._load_log(self.stability_log, n=5)
        if len(records) < 3:
            return "stable"
        scores_list = [float(r.get("long_running_stability_score", 75.0)) for r in records]
        mid       = len(scores_list) // 2
        avg_first = sum(scores_list[:mid]) / mid
        avg_last  = sum(scores_list[mid:]) / (len(scores_list) - mid)
        delta     = avg_last - avg_first
        if abs(delta) < 3.0:
            return "stable"
        return "improving" if delta > 0 else "degrading"

    def _build_recommendation(
        self,
        score:       float,
        status:      str,
        decay:       bool,
        sources:     list[str],
        trend:       str,
    ) -> str:
        if status == "CRITICAL":
            return (
                f"CRITICO ({score:.0f}/100): decaimento severo detectado. "
                f"Fontes: {', '.join(sources) if sources else 'multiplas'}. "
                "Intervencao imediata necessaria."
            )
        if status == "DEGRADING":
            return (
                f"DEGRADANDO ({score:.0f}/100): decaimento ativo. "
                f"Fontes: {', '.join(sources) if sources else 'nenhuma identificada'}. "
                "Monitoramento intensivo e possivel reinicializacao de sessao."
            )
        if status == "DRIFTING":
            return (
                f"DRIFT DETECTADO ({score:.0f}/100): instabilidade moderada. "
                f"Trend={trend}. "
                f"{'Fontes: ' + ', '.join(sources) + '.' if sources else ''} "
                "Monitorar proximo ciclo."
            )
        return (
            f"Sistema estavel ({score:.0f}/100). "
            f"Trend={trend}. "
            f"{'Decaimento leve em: ' + ', '.join(sources) + '.' if sources else 'Todas as dimensoes saudaveis.'}"
        )

    # ── Persistence ─────────────────────────────────────────────────────────────

    def _persist(self, report: StabilityReport) -> None:
        try:
            self.stability_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.stability_log, "a") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

    def _push_metrics(self, report: StabilityReport) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_runtime_health.set(report.runtime_health_score)
            _prom_decay.set(report.operational_decay_score)
            _prom_stability.set(report.long_running_stability_score)
            _prom_consistency.set(report.runtime_consistency_score)
        except Exception:
            pass

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
        description="Long-Running Stability Monitor — Phase R R-4"
    )
    parser.add_argument("--json", action="store_true", help="Saida JSON")
    args = parser.parse_args()

    engine = LongRunningStabilityEngine()
    report = engine.evaluate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    status_icons = {
        "STABLE": "[OK]", "DRIFTING": "[~~]",
        "DEGRADING": "[!!]", "CRITICAL": "[XX]",
    }
    icon = status_icons.get(report.stability_status, "[??]")

    print(f"\nLong-Running Stability Monitor — Phase R R-4")
    print(f"  report_id:                   {report.report_id}")
    print(f"  stability_status:            {icon} {report.stability_status}")
    print(f"  long_running_stability_score:{report.long_running_stability_score:.1f}/100")
    print(f"  runtime_health_score:        {report.runtime_health_score:.1f}/100")
    print(f"  operational_decay_score:     {report.operational_decay_score:.1f}/100")
    print(f"  runtime_consistency_score:   {report.runtime_consistency_score:.1f}/100")
    print(f"  overall_trend:               {report.overall_trend}")
    print(f"  session_age_hours:           {report.session_age_hours:.2f}h")
    print(f"  decay_detected:              {'SIM' if report.decay_detected else 'nao'}")
    if report.decay_sources:
        print(f"  decay_sources:               {', '.join(report.decay_sources)}")
    print(f"\n  Dimensoes ({len(report.dimensions)}):")
    for dim in report.dimensions:
        bar = "#" * int(dim.score / 10)
        print(f"    {dim.name:<22} {dim.score:5.1f}  [{bar:<10}]  {dim.trend:<10}  {dim.detail}")
    print(f"\n  -> {report.recommendation}")
    print(f"\n  evaluated_at: {report.evaluated_at}")


if __name__ == "__main__":
    main()

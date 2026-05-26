"""
runtime_burnin_engine.py — Phase S S-1

Runtime Burn-In Engine.

Tracks uptime, monitors multi-hour stability, detects progressive drift,
and validates continuous operational integrity across 8 dimensions.

Scores produzidos:
  - burnin_stability_score:       avg of all dimension scores (0-100)
  - runtime_burnin_score:         weighted composite (0-100)
  - long_session_integrity_score: stability bonus for long sessions (0-100)

Dimensoes avaliadas (8 total):
  1. uptime:              session age from data/runtime_state.json
  2. restart_frequency:   data/startup_log.jsonl entries in last 24h
  3. governance_drift:    stddev of last 10 data/runtime_governance_log.jsonl scores
  4. replay_drift:        stddev of last 10 data/live_execution_replay_log.jsonl scores
  5. metrics_gap:         mtime of data/live_governance_summary.jsonl
  6. collector_stability: last data/collector_reliability_log.jsonl entry score
  7. scheduler_stability: data/governance_history.jsonl entries in last 2h
  8. runtime_decay:       operational_decay_score from data/stability_log.jsonl (inverted)

burnin_phase:
  WARMING_UP   uptime < 4h
  STABILIZING  uptime 4-24h
  BURN_IN      uptime 24-72h
  MATURE       uptime > 72h

burnin_status:
  HEALTHY    burnin_stability_score >= 75
  DRIFTING   >= 55
  DEGRADING  >= 35
  CRITICAL   < 35

CLI:
  python -m domains.crypto_coin.research.runtime_burnin_engine
  python -m domains.crypto_coin.research.runtime_burnin_engine --json
"""

from __future__ import annotations

import argparse
import json
import math
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BURNIN_LOG = Path("data/runtime_burnin_log.jsonl")

# Source logs (read-only)
RUNTIME_STATE          = Path("data/runtime_state.json")
STARTUP_LOG            = Path("data/startup_log.jsonl")
RUNTIME_GOV_LOG        = Path("data/runtime_governance_log.jsonl")
REPLAY_LOG             = Path("data/live_execution_replay_log.jsonl")
GOV_SUMMARY_LOG        = Path("data/live_governance_summary.jsonl")
COLLECTOR_LOG          = Path("data/collector_reliability_log.jsonl")
GOVERNANCE_HIST_LOG    = Path("data/governance_history.jsonl")
STABILITY_LOG          = Path("data/stability_log.jsonl")

# Prometheus (optional)
try:
    from api.burnin_metrics import (
        burnin_stability_score       as _prom_burnin_stability,
        runtime_burnin_score         as _prom_runtime_burnin,
        long_session_integrity_score as _prom_long_session,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Thresholds ───────────────────────────────────────────────────────────────────

SCORE_HEALTHY   = 75.0
SCORE_DRIFTING  = 55.0
SCORE_DEGRADING = 35.0

# Weights for runtime_burnin_score
WEIGHTS = {
    "uptime":              0.20,
    "governance_drift":    0.20,
    "metrics_gap":         0.15,
    "replay_drift":        0.15,
    "scheduler_stability": 0.10,
    "restart_frequency":   0.10,
    "collector_stability": 0.10,
    # runtime_decay is not in the weighted composite — included only in avg
}


# ── Data classes ─────────────────────────────────────────────────────────────────

@dataclass
class BurninDimension:
    name:   str
    value:  float
    score:  float   # 0-100 derived score for this dimension
    trend:  str     # stable | improving | degrading | unknown
    detail: str


@dataclass
class BurninReport:
    """Relatorio de burn-in operacional — Phase S S-1."""
    report_id:                    str
    burnin_stability_score:       float   # avg of dimension scores
    runtime_burnin_score:         float   # weighted composite
    long_session_integrity_score: float   # stability bonus for long sessions
    uptime_hours:                 float
    restart_count_24h:            int
    dimensions:                   list[BurninDimension]
    dominant_drift_source:        str | None  # dimension with lowest score
    burnin_phase:                 str   # WARMING_UP | STABILIZING | BURN_IN | MATURE
    burnin_status:                str   # HEALTHY | DRIFTING | DEGRADING | CRITICAL
    evaluated_at:                 str
    recommendation:               str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dimensions"] = [asdict(dim) for dim in self.dimensions]
        return d


# ── Engine ───────────────────────────────────────────────────────────────────────

class RuntimeBurninEngine:
    """
    S-1: Runtime Burn-In Engine.

    Avalia 8 dimensoes operacionais para detectar drift progressivo
    e validar integridade continua de sessoes de longa duracao.
    """

    def __init__(self, burnin_log: Path = BURNIN_LOG):
        self.burnin_log = burnin_log

    def evaluate(self) -> BurninReport:
        """Avalia o estado de burn-in e retorna relatorio."""
        report_id    = str(uuid.uuid4())[:10]
        uptime_hours = self._compute_uptime_hours()

        dimensions = self._evaluate_all_dimensions(uptime_hours)

        scores = [d.score for d in dimensions]
        burnin_stability_score = round(sum(scores) / len(scores), 1)

        # Weighted composite — only the 7 named dimensions
        dim_map = {d.name: d.score for d in dimensions}
        runtime_burnin_score = round(
            sum(dim_map.get(name, 0.0) * w for name, w in WEIGHTS.items()), 1
        )

        # Long session integrity — bonus for sustained uptime
        bonus = min(uptime_hours, 72.0) / 72.0 * 0.2
        long_session_integrity_score = round(
            min(burnin_stability_score * (1 + bonus), 100.0), 1
        )

        restart_count_24h = self._count_restarts_24h()

        burnin_phase = (
            "WARMING_UP"  if uptime_hours < 4   else
            "STABILIZING" if uptime_hours < 24  else
            "BURN_IN"     if uptime_hours < 72  else
            "MATURE"
        )

        burnin_status = (
            "HEALTHY"   if burnin_stability_score >= SCORE_HEALTHY   else
            "DRIFTING"  if burnin_stability_score >= SCORE_DRIFTING  else
            "DEGRADING" if burnin_stability_score >= SCORE_DEGRADING else
            "CRITICAL"
        )

        worst = min(dimensions, key=lambda d: d.score)
        dominant_drift_source = worst.name if worst.score < 75.0 else None

        recommendation = self._build_recommendation(
            burnin_stability_score, burnin_status, burnin_phase,
            dominant_drift_source, dimensions,
        )

        report = BurninReport(
            report_id                    = report_id,
            burnin_stability_score       = burnin_stability_score,
            runtime_burnin_score         = runtime_burnin_score,
            long_session_integrity_score = long_session_integrity_score,
            uptime_hours                 = round(uptime_hours, 2),
            restart_count_24h            = restart_count_24h,
            dimensions                   = dimensions,
            dominant_drift_source        = dominant_drift_source,
            burnin_phase                 = burnin_phase,
            burnin_status                = burnin_status,
            evaluated_at                 = datetime.now(timezone.utc).isoformat(),
            recommendation               = recommendation,
        )

        self._persist(report)
        self._push_metrics(report)
        return report

    # ── Dimension evaluators ─────────────────────────────────────────────────────

    def _evaluate_all_dimensions(self, uptime_hours: float) -> list[BurninDimension]:
        return [
            self._dim_uptime(uptime_hours),
            self._dim_restart_frequency(),
            self._dim_governance_drift(),
            self._dim_replay_drift(),
            self._dim_metrics_gap(),
            self._dim_collector_stability(),
            self._dim_scheduler_stability(),
            self._dim_runtime_decay(),
        ]

    def _dim_uptime(self, uptime_hours: float) -> BurninDimension:
        if uptime_hours >= 72:
            score, trend = 100.0, "stable"
            detail = f"uptime={uptime_hours:.1f}h (MATURE)"
        elif uptime_hours >= 24:
            score, trend = 80.0, "stable"
            detail = f"uptime={uptime_hours:.1f}h (BURN_IN)"
        elif uptime_hours >= 4:
            score, trend = 60.0, "improving"
            detail = f"uptime={uptime_hours:.1f}h (STABILIZING)"
        elif uptime_hours >= 1:
            score, trend = 30.0, "improving"
            detail = f"uptime={uptime_hours:.1f}h (WARMING_UP)"
        else:
            score, trend = 0.0, "unknown"
            detail = f"uptime={uptime_hours:.2f}h (recém iniciado)"
        return BurninDimension(
            name="uptime", value=round(uptime_hours, 2),
            score=score, trend=trend, detail=detail,
        )

    def _dim_restart_frequency(self) -> BurninDimension:
        count = self._count_restarts_24h()
        if count == 0:
            score, trend = 100.0, "stable"
            detail = "0 restarts nas ultimas 24h"
        elif count == 1:
            score, trend = 70.0, "stable"
            detail = f"{count} restart nas ultimas 24h"
        elif count <= 3:
            score, trend = 40.0, "degrading"
            detail = f"{count} restarts nas ultimas 24h (elevado)"
        else:
            score, trend = 10.0, "degrading"
            detail = f"{count} restarts nas ultimas 24h (critico)"
        return BurninDimension(
            name="restart_frequency", value=float(count),
            score=score, trend=trend, detail=detail,
        )

    def _dim_governance_drift(self) -> BurninDimension:
        records = self._load_log(RUNTIME_GOV_LOG, n=10)
        if len(records) < 2:
            return BurninDimension(
                name="governance_drift", value=0.0, score=75.0,
                trend="unknown",
                detail=f"poucos registros ({len(records)}) — estimativa padrao",
            )
        scores_list = [float(r.get("runtime_governance_score", 75.0)) for r in records]
        stddev = self._stddev(scores_list)
        if stddev < 2:
            score, trend = 100.0, "stable"
        elif stddev < 5:
            score, trend = 80.0, "stable"
        elif stddev < 10:
            score, trend = 50.0, "degrading"
        else:
            score, trend = 20.0, "degrading"
        detail = f"stddev={stddev:.2f} em {len(scores_list)} amostras"
        return BurninDimension(
            name="governance_drift", value=round(stddev, 3),
            score=score, trend=trend, detail=detail,
        )

    def _dim_replay_drift(self) -> BurninDimension:
        records = self._load_log(REPLAY_LOG, n=10)
        if len(records) < 2:
            return BurninDimension(
                name="replay_drift", value=0.0, score=75.0,
                trend="unknown",
                detail=f"poucos registros ({len(records)}) — estimativa padrao",
            )
        raw_scores = []
        for r in records:
            v = float(r.get("avg_fidelity_score", 0.75))
            # normaliza para 0-1 se necessario
            if v > 1.0:
                v /= 100.0
            raw_scores.append(v)
        stddev = self._stddev(raw_scores)
        if stddev < 0.02:
            score, trend = 100.0, "stable"
        elif stddev < 0.05:
            score, trend = 75.0, "stable"
        else:
            score, trend = 40.0, "degrading"
        detail = f"stddev={stddev:.4f} em {len(raw_scores)} amostras (escala 0-1)"
        return BurninDimension(
            name="replay_drift", value=round(stddev, 4),
            score=score, trend=trend, detail=detail,
        )

    def _dim_metrics_gap(self) -> BurninDimension:
        if not GOV_SUMMARY_LOG.exists():
            return BurninDimension(
                name="metrics_gap", value=-1.0, score=10.0,
                trend="degrading",
                detail="live_governance_summary.jsonl nao encontrado",
            )
        try:
            mtime    = GOV_SUMMARY_LOG.stat().st_mtime
            now      = datetime.now(timezone.utc).timestamp()
            age_secs = now - mtime
            age_min  = age_secs / 60.0
            if age_min < 5:
                score, trend = 100.0, "stable"
                detail = f"atualizado ha {age_min:.1f} min (recente)"
            elif age_min < 15:
                score, trend = 70.0, "stable"
                detail = f"atualizado ha {age_min:.1f} min"
            elif age_min < 60:
                score, trend = 40.0, "degrading"
                detail = f"atualizado ha {age_min:.1f} min (moderado)"
            else:
                score, trend = 10.0, "degrading"
                detail = f"atualizado ha {age_min:.1f} min (stale)"
        except Exception as exc:
            age_secs = -1.0
            score, trend = 75.0, "unknown"
            detail = f"erro ao verificar mtime: {exc}"
        return BurninDimension(
            name="metrics_gap", value=round(age_secs, 1) if age_secs >= 0 else -1.0,
            score=score, trend=trend, detail=detail,
        )

    def _dim_collector_stability(self) -> BurninDimension:
        records = self._load_log(COLLECTOR_LOG, n=1)
        if not records:
            return BurninDimension(
                name="collector_stability", value=75.0, score=75.0,
                trend="unknown",
                detail="collector_reliability_log.jsonl nao encontrado — padrao 75",
            )
        raw = float(records[-1].get("collector_reliability_score", 75.0))
        score = max(0.0, min(100.0, raw))
        trend = "stable" if score >= 70 else "degrading"
        detail = f"collector_reliability_score={score:.1f}"
        return BurninDimension(
            name="collector_stability", value=score,
            score=score, trend=trend, detail=detail,
        )

    def _dim_scheduler_stability(self) -> BurninDimension:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        records = self._load_log(GOVERNANCE_HIST_LOG, n=50)
        count = 0
        for r in records:
            ts_str = r.get("evaluated_at") or r.get("timestamp")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if dt >= cutoff:
                        count += 1
                except Exception:
                    pass
        if count >= 3:
            score, trend = 100.0, "stable"
            detail = f"{count} entradas nas ultimas 2h (estavel)"
        elif count >= 1:
            score, trend = 50.0, "degrading"
            detail = f"{count} entrada(s) nas ultimas 2h (irregular)"
        else:
            score, trend = 0.0, "degrading"
            detail = "0 entradas nas ultimas 2h (scheduler parado)"
        return BurninDimension(
            name="scheduler_stability", value=float(count),
            score=score, trend=trend, detail=detail,
        )

    def _dim_runtime_decay(self) -> BurninDimension:
        records = self._load_log(STABILITY_LOG, n=1)
        if not records:
            return BurninDimension(
                name="runtime_decay", value=0.0, score=75.0,
                trend="unknown",
                detail="stability_log.jsonl nao encontrado — padrao decay=25",
            )
        decay = float(records[-1].get("operational_decay_score", 25.0))
        decay = max(0.0, min(100.0, decay))
        score = 100.0 - decay   # invertido: baixo decay = alta pontuacao
        trend = "stable" if decay <= 25 else ("degrading" if decay >= 50 else "stable")
        detail = f"operational_decay_score={decay:.1f} → score={score:.1f}"
        return BurninDimension(
            name="runtime_decay", value=decay,
            score=round(score, 1), trend=trend, detail=detail,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _compute_uptime_hours(self) -> float:
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

    def _count_restarts_24h(self) -> int:
        cutoff  = datetime.now(timezone.utc) - timedelta(hours=24)
        records = self._load_log(STARTUP_LOG, n=200)
        count   = 0
        for r in records:
            ts_str = r.get("started_at") or r.get("evaluated_at") or r.get("timestamp")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if dt >= cutoff:
                        count += 1
                except Exception:
                    pass
        return count

    def _stddev(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return math.sqrt(variance)

    def _build_recommendation(
        self,
        score:     float,
        status:    str,
        phase:     str,
        worst_dim: str | None,
        dims:      list[BurninDimension],
    ) -> str:
        worst_detail = ""
        if worst_dim:
            for d in dims:
                if d.name == worst_dim:
                    worst_detail = f" Pior dimensao: {worst_dim} ({d.score:.0f}/100 — {d.detail})."
                    break

        if status == "CRITICAL":
            return (
                f"CRITICO ({score:.0f}/100) [{phase}]: falha grave detectada.{worst_detail} "
                "Intervencao imediata necessaria."
            )
        if status == "DEGRADING":
            return (
                f"DEGRADANDO ({score:.0f}/100) [{phase}]: drift operacional ativo.{worst_detail} "
                "Monitorar intensivamente; considerar reinicio de sessao."
            )
        if status == "DRIFTING":
            return (
                f"DRIFT MODERADO ({score:.0f}/100) [{phase}]: instabilidade detectada.{worst_detail} "
                "Monitorar proximo ciclo."
            )
        return (
            f"SAUDAVEL ({score:.0f}/100) [{phase}]: sistema estavel. "
            f"{'Todas as dimensoes dentro do esperado.' if not worst_dim else f'Atencao a {worst_dim}.'}"
        )

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _persist(self, report: BurninReport) -> None:
        try:
            self.burnin_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.burnin_log, "a") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

    def _push_metrics(self, report: BurninReport) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_burnin_stability.set(report.burnin_stability_score)
            _prom_runtime_burnin.set(report.runtime_burnin_score)
            _prom_long_session.set(report.long_session_integrity_score)
        except Exception:
            pass

    def _load_log(self, path: Path, n: int = 10) -> list[dict]:
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
        description="Runtime Burn-In Engine — Phase S S-1"
    )
    parser.add_argument("--json", action="store_true", help="Saida JSON")
    args = parser.parse_args()

    engine = RuntimeBurninEngine()
    report = engine.evaluate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    status_icons = {
        "HEALTHY":   "[OK]",
        "DRIFTING":  "[~~]",
        "DEGRADING": "[!!]",
        "CRITICAL":  "[XX]",
    }
    icon = status_icons.get(report.burnin_status, "[??]")

    print(f"\nRuntime Burn-In Engine — Phase S S-1")
    print(f"  report_id:                   {report.report_id}")
    print(f"  burnin_status:               {icon} {report.burnin_status}")
    print(f"  burnin_phase:                {report.burnin_phase}")
    print(f"  burnin_stability_score:      {report.burnin_stability_score:.1f}/100")
    print(f"  runtime_burnin_score:        {report.runtime_burnin_score:.1f}/100")
    print(f"  long_session_integrity:      {report.long_session_integrity_score:.1f}/100")
    print(f"  uptime_hours:                {report.uptime_hours:.2f}h")
    print(f"  restart_count_24h:           {report.restart_count_24h}")
    if report.dominant_drift_source:
        print(f"  dominant_drift_source:       {report.dominant_drift_source}")

    print(f"\n  Dimensoes ({len(report.dimensions)}):")
    for dim in report.dimensions:
        bar    = "#" * int(dim.score / 10)
        marker = " <-- drift" if dim.name == report.dominant_drift_source else ""
        print(
            f"    {dim.name:<22} {dim.score:5.1f}  [{bar:<10}]"
            f"  {dim.trend:<10}  {dim.detail}{marker}"
        )

    print(f"\n  -> {report.recommendation}")
    print(f"\n  evaluated_at: {report.evaluated_at}")


if __name__ == "__main__":
    main()

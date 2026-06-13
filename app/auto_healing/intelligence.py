"""Phase 5: Operational Intelligence for AutoHealingWatchdog.

Responsibilities:
- TopCausesAnalyzer     — ranks services by incident frequency (7d / 30d)
- WorstServicesRanker   — combines reliability score + MTTR + incident count
- HealerEffectivenessAnalyzer — success/failure/MTTR reduction per healer
- RiskScorer            — LOW / MEDIUM / HIGH / CRITICAL risk per service
- RecommendationsEngine — rule-based actionable recommendations
- ExecutiveReporter     — weekly executive report (text + Telegram digest)

Rules:
- Does NOT import or modify healers, cooldown, or circuit breaker.
- Observation and recommendation only — zero side effects.
- All methods fail-silent when analytics data is unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# ── Risk levels ───────────────────────────────────────────────────────────────

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_CRITICAL = "CRITICAL"

_RISK_THRESHOLDS = {
    RISK_CRITICAL: 75.0,
    RISK_HIGH: 50.0,
    RISK_MEDIUM: 25.0,
}


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class IncidentCause:
    service: str
    incident_count: int
    pct_of_total: float
    avg_duration_seconds: float | None
    window_days: int

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["avg_duration_seconds"] is not None:
            d["avg_duration_seconds"] = round(d["avg_duration_seconds"], 1)
        d["pct_of_total"] = round(d["pct_of_total"], 1)
        return d


@dataclass
class ServiceRanking:
    service: str
    reliability_score: float
    grade: str
    mttr_avg_seconds: float | None
    incident_count: int
    heal_success_rate: float | None
    risk: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reliability_score"] = round(d["reliability_score"], 2)
        if d["mttr_avg_seconds"] is not None:
            d["mttr_avg_seconds"] = round(d["mttr_avg_seconds"], 1)
        if d["heal_success_rate"] is not None:
            d["heal_success_rate"] = round(d["heal_success_rate"], 3)
        return d


@dataclass
class HealerEffectivenessReport:
    healer: str
    target_service: str
    attempts: int
    recovered: int
    failed: int
    skipped: int
    blocked_circuit: int
    success_rate: float
    # estimated seconds saved per recovery vs no-healer (rough: avg_mttr * healed_fraction)
    estimated_mttr_reduction_seconds: float | None
    verdict: str   # "reliable" | "needs_investigation" | "insufficient_data" | "unreliable"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["success_rate"] = round(d["success_rate"], 3)
        if d["estimated_mttr_reduction_seconds"] is not None:
            d["estimated_mttr_reduction_seconds"] = round(
                d["estimated_mttr_reduction_seconds"], 1
            )
        return d


@dataclass
class Recommendation:
    service: str
    priority: str       # "HIGH" | "MEDIUM" | "LOW"
    category: str       # "healer" | "reliability" | "risk" | "forecast" | "operational"
    message: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskAssessment:
    service: str
    risk: str               # LOW / MEDIUM / HIGH / CRITICAL
    risk_score: float       # 0-100 (higher = worse)
    factors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_score"] = round(d["risk_score"], 1)
        return d


@dataclass
class WeeklyExecutiveReport:
    period_start: str
    period_end: str
    generated_at: str
    # Global KPIs
    overall_reliability_score: float
    overall_grade: str
    overall_risk: str
    incidents_total: int
    recoveries_total: int
    heal_success_rate: float | None
    recovery_rate: float | None
    mttr_avg_seconds: float | None
    # Rankings
    top_causes_7d: list[dict] = field(default_factory=list)
    top_causes_30d: list[dict] = field(default_factory=list)
    worst_services: list[dict] = field(default_factory=list)
    healer_effectiveness: list[dict] = field(default_factory=list)
    # Risk
    risk_by_service: dict[str, dict] = field(default_factory=dict)
    forecast_risks: list[str] = field(default_factory=list)
    # Recommendations
    recommendations: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self) -> str:
        """Human-readable weekly executive report."""
        lines = [
            "📋 AutoHealing — Relatório Executivo Semanal",
            f"📅 Período: {self.period_start} → {self.period_end}",
            "",
            "━━━ VISÃO GERAL ━━━",
            f"🏆 Reliability:    {self.overall_reliability_score:.1f}/100 ({self.overall_grade})",
            f"⚠️  Risco global:   {self.overall_risk}",
            f"🔴 Incidentes:     {self.incidents_total}",
            f"🟢 Recoveries:     {self.recoveries_total}",
        ]
        if self.heal_success_rate is not None:
            lines.append(f"📈 Heal success:   {self.heal_success_rate * 100:.1f}%")
        if self.mttr_avg_seconds is not None:
            mins = self.mttr_avg_seconds / 60
            lines.append(f"⏱  MTTR médio:     {mins:.1f} min")
        if self.top_causes_7d:
            lines += ["", "━━━ TOP CAUSAS (7d) ━━━"]
            for i, c in enumerate(self.top_causes_7d[:5], 1):
                svc = c["service"]
                cnt = c["incident_count"]
                pct = c["pct_of_total"]
                lines.append(f"  {i}. {svc}: {cnt} incidentes ({pct:.0f}%)")
        if self.worst_services:
            lines += ["", "━━━ PIORES SERVIÇOS ━━━"]
            for svc in self.worst_services[:5]:
                score = svc["reliability_score"]
                grade = svc["grade"]
                risk = svc["risk"]
                lines.append(f"  • {svc['service']}: {score:.0f}/100 {grade} [{risk}]")
        if self.healer_effectiveness:
            lines += ["", "━━━ EFICÁCIA DOS HEALERS ━━━"]
            for h in self.healer_effectiveness:
                rate = h["success_rate"] * 100
                verdict = h["verdict"]
                lines.append(f"  • {h['healer']}: {rate:.0f}% → {verdict}")
        if self.forecast_risks:
            lines += ["", "━━━ ALERTAS DE FORECAST ━━━"]
            for fr in self.forecast_risks:
                lines.append(f"  ⚠️  {fr}")
        if self.recommendations:
            lines += ["", "━━━ RECOMENDAÇÕES ━━━"]
            high = [r for r in self.recommendations if r.get("priority") == "HIGH"]
            med = [r for r in self.recommendations if r.get("priority") == "MEDIUM"]
            shown = high[:3] + med[:2]
            for r in shown:
                pri = r["priority"]
                svc = r["service"]
                msg = r["message"]
                lines.append(f"  [{pri}] {svc}: {msg}")
        lines.append("")
        lines.append(f"🕐 Gerado em: {self.generated_at}")
        return "\n".join(lines)

    def to_telegram(self, mode: str = "daily") -> str:
        """Compact Telegram digest — daily=shorter, weekly=full executive."""
        if mode == "daily":
            lines = [
                f"🤖 *AutoHealing Digest* — {self.period_end}",
                "",
                f"Reliability: *{self.overall_reliability_score:.0f}/100* {self.overall_grade} | Risco: *{self.overall_risk}*",
                f"Incidentes: {self.incidents_total} | Recoveries: {self.recoveries_total}",
            ]
            if self.heal_success_rate is not None:
                lines.append(f"Heal rate: {self.heal_success_rate * 100:.0f}%")
            if self.recommendations:
                high = [r for r in self.recommendations if r.get("priority") == "HIGH"]
                if high:
                    lines.append("")
                    lines.append("⚠️ *Ação necessária:*")
                    for r in high[:2]:
                        lines.append(f"  • [{r['service']}] {r['message']}")
        else:
            # Weekly — use full text minus header
            full = self.to_text()
            lines = ["*" + full.split("\n")[0].strip("📋 ") + "*"] + full.split("\n")[1:]
        return "\n".join(lines)


# ── Top Incident Causes ───────────────────────────────────────────────────────

class TopCausesAnalyzer:
    """Rank services by incident frequency over different windows."""

    def __init__(self, history_reader=None):
        self._reader = history_reader

    def _reader_instance(self):
        if self._reader is not None:
            return self._reader
        from app.auto_healing.analytics import HistoryReader
        return HistoryReader()

    def analyze(self, window_days: int = 7) -> list[IncidentCause]:
        """Return incident causes ranked by frequency for the given window."""
        try:
            reader = self._reader_instance()
            window_hours = window_days * 24
            incidents = reader.extract_incidents(window_hours=window_hours)
            if not incidents:
                return []

            # Count per service
            counts: dict[str, int] = {}
            durations: dict[str, list[float]] = {}
            for inc in incidents:
                svc = inc.service
                counts[svc] = counts.get(svc, 0) + 1
                if inc.duration_seconds is not None:
                    durations.setdefault(svc, []).append(inc.duration_seconds)

            total = sum(counts.values())
            results: list[IncidentCause] = []
            for svc, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                durs = durations.get(svc, [])
                avg_dur = sum(durs) / len(durs) if durs else None
                results.append(IncidentCause(
                    service=svc,
                    incident_count=cnt,
                    pct_of_total=cnt / total * 100 if total > 0 else 0.0,
                    avg_duration_seconds=avg_dur,
                    window_days=window_days,
                ))
            return results
        except Exception as exc:
            logger.warning("TopCausesAnalyzer.analyze failed: %s", exc)
            return []


# ── Worst Services Ranker ─────────────────────────────────────────────────────

class WorstServicesRanker:
    """Combine reliability, MTTR, incident count to rank worst services."""

    def __init__(self, reliability_scorer=None, history_reader=None):
        self._scorer = reliability_scorer
        self._reader = history_reader

    def rank(self, window_hours: int = 168, top_n: int = 10) -> list[ServiceRanking]:
        """Return services sorted worst-first by a composite badness score."""
        try:
            from app.auto_healing.analytics import HistoryReader
            from app.auto_healing.reliability import ReliabilityScorer

            reader = self._reader or HistoryReader()
            scorer = self._scorer or ReliabilityScorer(window_hours=window_hours, history_reader=reader)

            scores = scorer.score_all()
            mttr_map = reader.compute_mttr(window_hours=window_hours)
            incidents = reader.extract_incidents(window_hours=window_hours)

            inc_counts: dict[str, int] = {}
            for inc in incidents:
                inc_counts[inc.service] = inc_counts.get(inc.service, 0) + 1

            # Only rank services that have had at least 1 incident or score < 100
            candidates = set(inc_counts.keys()) | {
                svc for svc, s in scores.items() if s.score < 100.0
            }

            risk_scorer = RiskScorer(
                reliability_scorer=self._scorer,
                history_reader=self._reader,
            )
            risk_map = {r.service: r for r in risk_scorer.score_all(window_hours=window_hours)}

            rankings: list[ServiceRanking] = []
            for svc in candidates:
                score_obj = scores.get(svc)
                score_val = score_obj.score if score_obj else 100.0
                grade = score_obj.grade if score_obj else "A+"
                mttr_data = mttr_map.get(svc, {})
                mttr_avg = mttr_data.get("avg_seconds")
                inc_cnt = inc_counts.get(svc, 0)
                # heal success rate from history
                healer_stats = reader.healer_stats(window_hours=window_hours)
                hsr = None
                for h in healer_stats:
                    if h["target_service"] == svc and h["attempts"] > 0:
                        hsr = h["success_rate"]
                        break
                risk_obj = risk_map.get(svc)
                risk = risk_obj.risk if risk_obj else RISK_LOW

                rankings.append(ServiceRanking(
                    service=svc,
                    reliability_score=score_val,
                    grade=grade,
                    mttr_avg_seconds=mttr_avg,
                    incident_count=inc_cnt,
                    heal_success_rate=hsr,
                    risk=risk,
                ))

            # Sort: CRITICAL > HIGH > score ascending > incidents descending
            _risk_order = {RISK_CRITICAL: 0, RISK_HIGH: 1, RISK_MEDIUM: 2, RISK_LOW: 3}
            rankings.sort(key=lambda r: (_risk_order.get(r.risk, 3), r.reliability_score, -r.incident_count))
            return rankings[:top_n]
        except Exception as exc:
            logger.warning("WorstServicesRanker.rank failed: %s", exc)
            return []


# ── Healer Effectiveness ──────────────────────────────────────────────────────

class HealerEffectivenessAnalyzer:
    """Extend healer_stats with MTTR reduction estimates and verdicts."""

    _MIN_ATTEMPTS_FOR_VERDICT = 2

    def __init__(self, history_reader=None):
        self._reader = history_reader

    def analyze(self, window_hours: int = 168) -> list[HealerEffectivenessReport]:
        try:
            from app.auto_healing.analytics import HistoryReader
            reader = self._reader or HistoryReader()
            stats = reader.healer_stats(window_hours=window_hours)
            mttr_map = reader.compute_mttr(window_hours=window_hours)

            results: list[HealerEffectivenessReport] = []
            for h in stats:
                healer = h["healer"]
                target = h["target_service"]
                attempts = h["attempts"]
                recovered = h["recovered"]
                failed = h["failed"]
                skipped = h["skipped"]
                blocked = h["blocked_circuit"]
                rate = h["success_rate"]

                # Estimate MTTR reduction: if healer recovered N times,
                # it saved roughly avg_mttr * N seconds that would have been
                # longer without healing.  Rough proxy only.
                mttr_data = mttr_map.get(target, {})
                mttr_avg = mttr_data.get("avg_seconds")
                estimated_reduction = (
                    mttr_avg * recovered * 0.5 if (mttr_avg and recovered > 0) else None
                )

                verdict = self._verdict(attempts, rate, blocked)
                results.append(HealerEffectivenessReport(
                    healer=healer,
                    target_service=target,
                    attempts=attempts,
                    recovered=recovered,
                    failed=failed,
                    skipped=skipped,
                    blocked_circuit=blocked,
                    success_rate=rate,
                    estimated_mttr_reduction_seconds=estimated_reduction,
                    verdict=verdict,
                ))
            # Sort: worst (lowest success_rate) first
            results.sort(key=lambda r: r.success_rate)
            return results
        except Exception as exc:
            logger.warning("HealerEffectivenessAnalyzer.analyze failed: %s", exc)
            return []

    def _verdict(self, attempts: int, success_rate: float, blocked_circuit: int) -> str:
        if attempts < self._MIN_ATTEMPTS_FOR_VERDICT:
            return "insufficient_data"
        if success_rate >= 0.8:
            return "reliable"
        if success_rate >= 0.5:
            return "moderate"
        if blocked_circuit > 0 and success_rate < 0.5:
            return "circuit_tripped"
        return "needs_investigation"


# ── Risk Scorer ───────────────────────────────────────────────────────────────

class RiskScorer:
    """Compute a risk level (LOW/MEDIUM/HIGH/CRITICAL) per service.

    Risk score (0-100, higher = worse):
      + (1 - reliability_score/100) * 40   → reliability component (max 40)
      + min(incident_count, 6) * 5         → incident volume (max 30)
      + mttr_minutes * 0.5 capped 20       → recovery speed (max 20)
      + (1 - heal_success_rate) * 10 if known (max 10)

    Thresholds: CRITICAL ≥ 75, HIGH ≥ 50, MEDIUM ≥ 25, LOW < 25
    """

    def __init__(self, reliability_scorer=None, history_reader=None):
        self._scorer = reliability_scorer
        self._reader = history_reader

    def score_all(self, window_hours: int = 168) -> list[RiskAssessment]:
        try:
            from app.auto_healing.analytics import HistoryReader
            from app.auto_healing.reliability import ReliabilityScorer

            reader = self._reader or HistoryReader()
            scorer = self._scorer or ReliabilityScorer(window_hours=window_hours, history_reader=reader)

            scores = scorer.score_all()
            mttr_map = reader.compute_mttr(window_hours=window_hours)
            incidents = reader.extract_incidents(window_hours=window_hours)
            healer_stats = reader.healer_stats(window_hours=window_hours)

            inc_counts: dict[str, int] = {}
            for inc in incidents:
                inc_counts[inc.service] = inc_counts.get(inc.service, 0) + 1

            hsr_map: dict[str, float] = {}
            for h in healer_stats:
                if h["attempts"] > 0:
                    hsr_map[h["target_service"]] = h["success_rate"]

            all_services = set(scores.keys()) | set(inc_counts.keys())
            results: list[RiskAssessment] = []
            for svc in all_services:
                score_obj = scores.get(svc)
                rel_score = score_obj.score if score_obj else 100.0
                inc_cnt = inc_counts.get(svc, 0)
                mttr_data = mttr_map.get(svc, {})
                mttr_avg = mttr_data.get("avg_seconds")
                hsr = hsr_map.get(svc)

                assessment = self._compute(svc, rel_score, inc_cnt, mttr_avg, hsr)
                results.append(assessment)

            results.sort(key=lambda r: r.risk_score, reverse=True)
            return results
        except Exception as exc:
            logger.warning("RiskScorer.score_all failed: %s", exc)
            return []

    @staticmethod
    def _compute(
        service: str,
        reliability_score: float,
        incident_count: int,
        mttr_avg_seconds: float | None,
        heal_success_rate: float | None,
    ) -> RiskAssessment:
        risk_score = 0.0
        factors: list[str] = []

        # Component 1: reliability
        rel_component = (1.0 - reliability_score / 100.0) * 40.0
        if rel_component > 0:
            risk_score += rel_component
            factors.append(f"reliability={reliability_score:.0f}/100")

        # Component 2: incident volume
        inc_component = min(incident_count, 6) * 5.0
        if inc_component > 0:
            risk_score += inc_component
            factors.append(f"incidents={incident_count}")

        # Component 3: MTTR
        if mttr_avg_seconds is not None:
            mttr_component = min(mttr_avg_seconds / 60.0 * 0.5, 20.0)
            risk_score += mttr_component
            if mttr_component > 5:
                factors.append(f"mttr={mttr_avg_seconds:.0f}s")

        # Component 4: heal success rate
        if heal_success_rate is not None:
            hsr_component = (1.0 - heal_success_rate) * 10.0
            risk_score += hsr_component
            if hsr_component > 3:
                factors.append(f"heal_rate={heal_success_rate * 100:.0f}%")

        risk_score = round(min(100.0, risk_score), 1)

        if risk_score >= _RISK_THRESHOLDS[RISK_CRITICAL]:
            level = RISK_CRITICAL
        elif risk_score >= _RISK_THRESHOLDS[RISK_HIGH]:
            level = RISK_HIGH
        elif risk_score >= _RISK_THRESHOLDS[RISK_MEDIUM]:
            level = RISK_MEDIUM
        else:
            level = RISK_LOW

        if not factors:
            factors = ["sem incidentes recentes"]

        return RiskAssessment(service=service, risk=level, risk_score=risk_score, factors=factors)


# ── Recommendations Engine ────────────────────────────────────────────────────

class RecommendationsEngine:
    """Rule-based recommendations from operational data.

    Rules fire on hard thresholds — no ML, deterministic.
    Rules are independent; multiple may fire per service.
    """

    def __init__(self, history_reader=None, reliability_scorer=None):
        self._reader = history_reader
        self._scorer = reliability_scorer

    def generate(self, window_hours: int = 168) -> list[Recommendation]:
        try:
            from app.auto_healing.analytics import HistoryReader
            from app.auto_healing.reliability import ReliabilityScorer

            reader = self._reader or HistoryReader()
            scorer = self._scorer or ReliabilityScorer(window_hours=window_hours, history_reader=reader)

            scores = scorer.score_all()
            healer_stats = reader.healer_stats(window_hours=window_hours)
            mttr_map = reader.compute_mttr(window_hours=window_hours)
            incidents = reader.extract_incidents(window_hours=window_hours)
            risk_scorer = RiskScorer(reliability_scorer=self._scorer, history_reader=self._reader)
            risk_assessments = {r.service: r for r in risk_scorer.score_all(window_hours=window_hours)}

            inc_counts: dict[str, int] = {}
            for inc in incidents:
                inc_counts[inc.service] = inc_counts.get(inc.service, 0) + 1

            recs: list[Recommendation] = []

            # ── Healer rules ──────────────────────────────────────────────────
            for h in healer_stats:
                svc = h["target_service"]
                attempts = h["attempts"]
                rate = h["success_rate"]
                blocked = h["blocked_circuit"]
                failed = h["failed"]

                if attempts >= 3 and rate < 0.33:
                    recs.append(Recommendation(
                        service=svc,
                        priority="HIGH",
                        category="healer",
                        message=(
                            f"Healer '{h['healer']}' com success_rate={rate * 100:.0f}% "
                            f"após {attempts} tentativas — investigar root cause imediatamente"
                        ),
                        evidence={"attempts": attempts, "success_rate": rate, "failed": failed},
                    ))
                elif attempts >= 2 and 0.33 <= rate < 0.5:
                    recs.append(Recommendation(
                        service=svc,
                        priority="MEDIUM",
                        category="healer",
                        message=(
                            f"Healer '{h['healer']}' com success_rate={rate * 100:.0f}% — "
                            "monitorar; pode indicar problema recorrente"
                        ),
                        evidence={"attempts": attempts, "success_rate": rate},
                    ))
                elif attempts >= 2 and rate >= 0.9:
                    recs.append(Recommendation(
                        service=svc,
                        priority="LOW",
                        category="healer",
                        message=(
                            f"Healer '{h['healer']}' confiável — success_rate={rate * 100:.0f}% "
                            f"em {attempts} tentativas; manter configuração atual"
                        ),
                        evidence={"attempts": attempts, "success_rate": rate, "recovered": h["recovered"]},
                    ))
                if blocked > 0:
                    recs.append(Recommendation(
                        service=svc,
                        priority="MEDIUM",
                        category="healer",
                        message=(
                            f"Circuit breaker abriu {blocked}x para '{h['healer']}' — "
                            "revisar threshold de falhas e intervalo entre tentativas"
                        ),
                        evidence={"blocked_circuit": blocked, "attempts": attempts},
                    ))

            # ── Reliability rules ─────────────────────────────────────────────
            for svc, score_obj in scores.items():
                score = score_obj.score
                if score < 60:
                    recs.append(Recommendation(
                        service=svc,
                        priority="HIGH",
                        category="reliability",
                        message=(
                            f"Score crítico {score:.0f}/100 ({score_obj.grade}) — "
                            "investigar causa raiz do downtime recorrente"
                        ),
                        evidence={"score": score, "grade": score_obj.grade,
                                  "uptime_pct": score_obj.uptime_pct},
                    ))
                elif score < 80:
                    recs.append(Recommendation(
                        service=svc,
                        priority="MEDIUM",
                        category="reliability",
                        message=(
                            f"Score degradado {score:.0f}/100 ({score_obj.grade}) — "
                            "monitorar tendência e revisar alertas"
                        ),
                        evidence={"score": score, "grade": score_obj.grade},
                    ))

            # ── MTTR rules ────────────────────────────────────────────────────
            for svc, mttr_data in mttr_map.items():
                avg = mttr_data.get("avg_seconds", 0)
                if avg > 3600:
                    recs.append(Recommendation(
                        service=svc,
                        priority="HIGH",
                        category="operational",
                        message=(
                            f"MTTR médio = {avg / 60:.0f} min — "
                            "healer pode não estar resolvendo a causa raiz; "
                            "verificar se restart resolve ou apenas mascara"
                        ),
                        evidence={"mttr_avg_seconds": avg, "count": mttr_data.get("count")},
                    ))
                elif avg > 1800:
                    recs.append(Recommendation(
                        service=svc,
                        priority="MEDIUM",
                        category="operational",
                        message=(
                            f"MTTR médio = {avg / 60:.0f} min — "
                            "considerar investigação de causa raiz"
                        ),
                        evidence={"mttr_avg_seconds": avg},
                    ))

            # ── Risk rules ────────────────────────────────────────────────────
            for svc, risk_obj in risk_assessments.items():
                if risk_obj.risk == RISK_CRITICAL:
                    recs.append(Recommendation(
                        service=svc,
                        priority="HIGH",
                        category="risk",
                        message=(
                            f"Risco CRITICAL (score={risk_obj.risk_score:.0f}) — "
                            "ação imediata necessária"
                        ),
                        evidence={"risk_score": risk_obj.risk_score, "factors": risk_obj.factors},
                    ))

            # ── Forecast rules ────────────────────────────────────────────────
            try:
                from app.auto_healing.reliability import Forecaster
                fc = Forecaster().forecast(window_hours=48.0)
                for series_name, data in [
                    ("disk", fc.disk), ("memory", fc.memory), ("queue_backlog", fc.queue_backlog)
                ]:
                    if not data:
                        continue
                    eta = data.get("eta_threshold_hours")
                    status = data.get("status", "")
                    if status == "critical" or (eta is not None and eta < 24):
                        recs.append(Recommendation(
                            service="infrastructure",
                            priority="HIGH",
                            category="forecast",
                            message=(
                                f"{series_name} crítico — "
                                f"ETA threshold em {eta:.1f}h" if eta else f"{series_name} acima do threshold"
                            ),
                            evidence={"series": series_name, "eta_hours": eta, "status": status},
                        ))
                    elif status == "warning" or (eta is not None and eta < 48):
                        recs.append(Recommendation(
                            service="infrastructure",
                            priority="MEDIUM",
                            category="forecast",
                            message=(
                                f"{series_name} em alerta — "
                                f"ETA threshold em {eta:.1f}h" if eta else f"{series_name} acima do threshold"
                            ),
                            evidence={"series": series_name, "eta_hours": eta, "status": status},
                        ))
            except Exception as fc_exc:
                logger.debug("recommendations: forecast check failed: %s", fc_exc)

            # De-duplicate: keep highest priority per (service, category, first 60 chars of message)
            seen: set[tuple] = set()
            deduped: list[Recommendation] = []
            _pri_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            recs.sort(key=lambda r: _pri_order.get(r.priority, 9))
            for rec in recs:
                key = (rec.service, rec.category, rec.message[:60])
                if key not in seen:
                    seen.add(key)
                    deduped.append(rec)

            return deduped
        except Exception as exc:
            logger.warning("RecommendationsEngine.generate failed: %s", exc)
            return []


# ── Executive Reporter ────────────────────────────────────────────────────────

class ExecutiveReporter:
    """Generate a weekly executive report combining all Phase 3-5 signals."""

    def __init__(
        self,
        history_reader=None,
        reliability_scorer=None,
    ):
        self._reader = history_reader
        self._scorer = reliability_scorer

    def generate(self, window_hours: int = 168) -> WeeklyExecutiveReport:
        """Build the weekly executive report. Fails gracefully."""
        now = datetime.now(tz=timezone.utc)
        period_end = now.isoformat()
        period_start = (now - timedelta(hours=window_hours)).isoformat()

        try:
            from app.auto_healing.analytics import HistoryReader
            from app.auto_healing.reliability import ReliabilityScorer, _grade

            reader = self._reader or HistoryReader()
            scorer = self._scorer or ReliabilityScorer(window_hours=window_hours, history_reader=reader)

            # ── Global metrics from history ───────────────────────────────────
            incidents = reader.extract_incidents(window_hours=window_hours)
            healer_stats = reader.healer_stats(window_hours=window_hours)
            mttr_map = reader.compute_mttr(window_hours=window_hours)

            incidents_total = len(incidents)
            recoveries_total = sum(1 for i in incidents if i.outcome == "recovered")
            total_attempts = sum(h["attempts"] for h in healer_stats)
            total_recovered = sum(h["recovered"] for h in healer_stats)
            heal_success_rate = (
                total_recovered / total_attempts if total_attempts > 0 else None
            )
            recovery_rate = (
                recoveries_total / incidents_total if incidents_total > 0 else None
            )

            # Weighted average MTTR
            all_durations = [
                dur for d in mttr_map.values()
                for dur in ([d["avg_seconds"]] * d.get("count", 1))
                if d.get("avg_seconds") is not None
            ]
            mttr_avg = sum(all_durations) / len(all_durations) if all_durations else None

            # ── Reliability scores ────────────────────────────────────────────
            scores = scorer.score_all()
            if scores:
                scored_services = [s for s in scores.values() if s.score is not None]
                overall_rel = (
                    sum(s.score for s in scored_services) / len(scored_services)
                    if scored_services else 100.0
                )
            else:
                overall_rel = 100.0
            overall_grade = _grade(overall_rel)

            # ── Sub-analyses ──────────────────────────────────────────────────
            top_causes_7d = TopCausesAnalyzer(history_reader=reader).analyze(window_days=7)
            top_causes_30d = TopCausesAnalyzer(history_reader=reader).analyze(window_days=30)
            worst = WorstServicesRanker(
                reliability_scorer=scorer, history_reader=reader
            ).rank(window_hours=window_hours)
            healer_eff = HealerEffectivenessAnalyzer(history_reader=reader).analyze(
                window_hours=window_hours
            )
            risk_scorer_inst = RiskScorer(
                reliability_scorer=scorer, history_reader=reader
            )
            risk_assessments = risk_scorer_inst.score_all(window_hours=window_hours)
            risk_by_service = {r.service: r.to_dict() for r in risk_assessments}

            # Overall risk = worst service risk
            _risk_order_w = {RISK_CRITICAL: 0, RISK_HIGH: 1, RISK_MEDIUM: 2, RISK_LOW: 3}
            overall_risk = (
                min(risk_assessments, key=lambda r: _risk_order_w.get(r.risk, 3)).risk
                if risk_assessments else RISK_LOW
            )

            # ── Forecast risks (text summary) ─────────────────────────────────
            forecast_risk_msgs: list[str] = []
            try:
                from app.auto_healing.reliability import Forecaster
                fc = Forecaster().forecast(window_hours=48.0)
                for _series_name, label, data in [
                    ("disk", "Disco", fc.disk),
                    ("memory", "Memória", fc.memory),
                    ("queue", "Fila", fc.queue_backlog),
                ]:
                    if not data:
                        continue
                    eta = data.get("eta_threshold_hours")
                    status = data.get("status", "")
                    if status in ("critical", "warning") or (eta is not None and eta < 48):
                        forecast_risk_msgs.append(
                            f"{label}: {status} — ETA threshold {eta:.1f}h"
                            if eta else f"{label}: {status}"
                        )
            except Exception:
                pass

            # ── Recommendations ───────────────────────────────────────────────
            recs = RecommendationsEngine(
                history_reader=reader, reliability_scorer=scorer
            ).generate(window_hours=window_hours)

            return WeeklyExecutiveReport(
                period_start=period_start,
                period_end=period_end,
                generated_at=now.isoformat(),
                overall_reliability_score=round(overall_rel, 2),
                overall_grade=overall_grade,
                overall_risk=overall_risk,
                incidents_total=incidents_total,
                recoveries_total=recoveries_total,
                heal_success_rate=heal_success_rate,
                recovery_rate=recovery_rate,
                mttr_avg_seconds=round(mttr_avg, 1) if mttr_avg else None,
                top_causes_7d=[c.to_dict() for c in top_causes_7d],
                top_causes_30d=[c.to_dict() for c in top_causes_30d],
                worst_services=[s.to_dict() for s in worst],
                healer_effectiveness=[h.to_dict() for h in healer_eff],
                risk_by_service=risk_by_service,
                forecast_risks=forecast_risk_msgs,
                recommendations=[r.to_dict() for r in recs],
            )

        except Exception as exc:
            logger.warning("ExecutiveReporter.generate failed: %s", exc)
            return WeeklyExecutiveReport(
                period_start=period_start,
                period_end=period_end,
                generated_at=now.isoformat(),
                overall_reliability_score=100.0,
                overall_grade="A+",
                overall_risk=RISK_LOW,
                incidents_total=0,
                recoveries_total=0,
                heal_success_rate=None,
                recovery_rate=None,
                mttr_avg_seconds=None,
            )

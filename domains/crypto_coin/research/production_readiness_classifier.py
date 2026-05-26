"""
production_readiness_classifier.py — Phase R R-9

Production Readiness Classifier.

Classifies the current environment readiness level based on all operational
signals. Evaluates 8 operational dimensions and assigns a classification from
DEVELOPMENT through PRODUCTION_READY.

Output JSONL: data/production_readiness_log.jsonl

Prometheus (optional, from api.runtime_metrics):
  readiness_confidence  Gauge

Classifications (best → worst):
  PRODUCTION_READY  uptime > 72h, incident_freq < 0.1/h, replay > 90, stability > 85,
                    governance > 85, watchdog > 85, recovery > 90
  MICRO_LIVE_READY  uptime > 24h, incident_freq < 0.5/h, replay > 80, stability > 75,
                    governance > 75, watchdog > 75, recovery > 80
  PAPER_STABLE      uptime > 4h, incident_freq < 2/h, stability > 60,
                    governance > 60, watchdog > 60
  RESEARCH          any uptime, stability > 40, basic checks pass
  DEGRADED          stability < 40 OR too many incidents
  FROZEN            any CRITICAL/EMERGENCY active incident OR guardian FROZEN/ROLLBACK
  DEVELOPMENT       default when insufficient data

CLI:
  python -m domains.crypto_coin.research.production_readiness_classifier
  python -m domains.crypto_coin.research.production_readiness_classifier --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

READINESS_LOG = Path("data/production_readiness_log.jsonl")

# Source data files (read-only)
RUNTIME_STATE_FILE  = Path("data/runtime_state.json")
INCIDENT_LOG        = Path("data/incident_log.jsonl")
ACTIVE_INCIDENTS    = Path("data/active_incidents.json")
REPLAY_LOG          = Path("data/live_execution_replay_log.jsonl")
STABILITY_LOG       = Path("data/stability_log.jsonl")
GOVERNANCE_LOG      = Path("data/runtime_governance_log.jsonl")
WATCHDOG_LOG        = Path("data/watchdog_log.jsonl")
RECOVERY_LOG        = Path("data/recovery_log.jsonl")
GUARDIAN_LOG        = Path("data/live_guardian_log.jsonl")

# Classification names ordered best → worst
CLASSIFICATIONS = [
    "PRODUCTION_READY",
    "MICRO_LIVE_READY",
    "PAPER_STABLE",
    "RESEARCH",
    "DEGRADED",
    "FROZEN",
    "DEVELOPMENT",
]

# Prometheus (optional)
try:
    from api.runtime_metrics import readiness_confidence as _prom_confidence
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ReadinessDimension:
    name:      str
    value:     float
    threshold: float
    passed:    bool
    weight:    float


@dataclass
class ProductionReadinessReport:
    report_id:                str
    classification:           str    # one of 7 classifications
    readiness_confidence:     float  # 0-100
    operational_maturity_score: float  # 0-100
    dimensions:               list[ReadinessDimension]
    dimensions_passed:        int
    dimensions_total:         int
    uptime_hours:             float
    incident_rate_per_hour:   float
    blocking_factors:         list[str]    # reasons why higher classification not achieved
    advancement_requirements: list[str]    # what's needed to advance one level
    classification_history:   list[str]    # last 5 classifications from log
    evaluated_at:             str
    recommendation:           str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dimensions"] = [asdict(dim) for dim in self.dimensions]
        return d


# ── Classifier ─────────────────────────────────────────────────────────────────

class ProductionReadinessClassifier:
    """
    R-9: Production Readiness Classifier.

    Evaluates operational dimensions from all Phase R subsystem logs and
    classifies the current readiness level.
    """

    def __init__(self, readiness_log: Path = READINESS_LOG):
        self.readiness_log = readiness_log

    def classify(self) -> ProductionReadinessReport:
        """Evaluate all dimensions and return a readiness classification."""
        report_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # ── Collect raw signals ───────────────────────────────────────────────
        uptime_hours        = self._get_uptime_hours()
        incident_rate       = self._get_incident_rate_per_hour()
        replay_integrity    = self._get_replay_integrity()
        stability_score     = self._get_stability_score()
        governance_score    = self._get_governance_score()
        watchdog_score      = self._get_watchdog_score()
        recovery_success    = self._get_recovery_success()
        guardian_state      = self._get_guardian_state()
        active_critical     = self._get_active_critical_count()

        # ── Check blocking conditions ─────────────────────────────────────────
        is_frozen = (
            active_critical > 0 or
            guardian_state in ("FROZEN", "ROLLBACK")
        )

        # ── Build dimensions ──────────────────────────────────────────────────
        # Thresholds are set for PAPER_STABLE as the baseline reference level
        dimensions: list[ReadinessDimension] = [
            ReadinessDimension(
                name="uptime_hours",
                value=uptime_hours,
                threshold=4.0,
                passed=uptime_hours >= 4.0,
                weight=1.0,
            ),
            ReadinessDimension(
                name="incident_rate_low",
                value=incident_rate,
                threshold=2.0,
                passed=incident_rate < 2.0,
                weight=1.0,
            ),
            ReadinessDimension(
                name="stability_score",
                value=stability_score,
                threshold=60.0,
                passed=stability_score >= 60.0,
                weight=1.0,
            ),
            ReadinessDimension(
                name="governance_score",
                value=governance_score,
                threshold=60.0,
                passed=governance_score >= 60.0,
                weight=1.0,
            ),
            ReadinessDimension(
                name="watchdog_score",
                value=watchdog_score,
                threshold=60.0,
                passed=watchdog_score >= 60.0,
                weight=1.0,
            ),
            ReadinessDimension(
                name="replay_integrity",
                value=replay_integrity,
                threshold=70.0,
                passed=replay_integrity >= 70.0,
                weight=1.0,
            ),
            ReadinessDimension(
                name="recovery_success",
                value=recovery_success,
                threshold=60.0,
                passed=recovery_success >= 60.0,
                weight=1.0,
            ),
            ReadinessDimension(
                name="no_critical_incidents",
                value=float(active_critical),
                threshold=0.0,
                passed=active_critical == 0,
                weight=1.0,
            ),
        ]

        # ── Operational maturity score (equal-weight average of passed dims) ──
        total_dims = len(dimensions)
        passed_dims = sum(1 for d in dimensions if d.passed)
        operational_maturity = round((passed_dims / total_dims) * 100.0, 1)

        # ── Classify ──────────────────────────────────────────────────────────
        classification, blocking_factors = self._determine_classification(
            is_frozen,
            uptime_hours,
            incident_rate,
            replay_integrity,
            stability_score,
            governance_score,
            watchdog_score,
            recovery_success,
            active_critical,
            guardian_state,
            operational_maturity,
        )

        # ── Readiness confidence ──────────────────────────────────────────────
        readiness_confidence = self._compute_confidence(
            operational_maturity, classification,
            uptime_hours, incident_rate, stability_score,
        )

        # ── Advancement requirements ──────────────────────────────────────────
        advancement_requirements = self._compute_advancement(
            classification,
            uptime_hours, incident_rate, replay_integrity,
            stability_score, governance_score, watchdog_score, recovery_success,
        )

        # ── Classification history ────────────────────────────────────────────
        classification_history = self._load_classification_history(n=5)

        recommendation = self._build_recommendation(
            classification, readiness_confidence, blocking_factors,
            advancement_requirements,
        )

        report = ProductionReadinessReport(
            report_id                 = report_id,
            classification            = classification,
            readiness_confidence      = readiness_confidence,
            operational_maturity_score = operational_maturity,
            dimensions                = dimensions,
            dimensions_passed         = passed_dims,
            dimensions_total          = total_dims,
            uptime_hours              = round(uptime_hours, 2),
            incident_rate_per_hour    = round(incident_rate, 4),
            blocking_factors          = blocking_factors,
            advancement_requirements  = advancement_requirements,
            classification_history    = classification_history,
            evaluated_at              = now,
            recommendation            = recommendation,
        )

        self._persist(report)

        if _METRICS_AVAILABLE:
            try:
                _prom_confidence.set(readiness_confidence)
            except Exception:
                pass

        return report

    # ── Signal collectors ──────────────────────────────────────────────────────

    def _get_uptime_hours(self) -> float:
        if not RUNTIME_STATE_FILE.exists():
            return 0.0
        try:
            with open(RUNTIME_STATE_FILE) as f:
                data = json.load(f)
            session_start = data.get("session_start", "")
            if not session_start:
                return 0.0
            start_dt = datetime.fromisoformat(session_start)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - start_dt
            return delta.total_seconds() / 3600.0
        except Exception:
            return 0.0

    def _get_incident_rate_per_hour(self) -> float:
        """Count incidents in the last hour from incident_log.jsonl."""
        if not INCIDENT_LOG.exists():
            return 0.0
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
            count = 0
            with open(INCIDENT_LOG) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        ts_str = record.get("timestamp", record.get("created_at", ""))
                        if not ts_str:
                            continue
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            count += 1
                    except Exception:
                        pass
            return float(count)
        except Exception:
            return 0.0

    def _get_replay_integrity(self) -> float:
        last = self._load_last(REPLAY_LOG)
        if last is None:
            return 75.0
        fidelity = last.get("avg_fidelity_score", last.get("fidelity_score", 0.75))
        # avg_fidelity_score may be 0-1 or 0-100
        value = float(fidelity)
        if value <= 1.0:
            value *= 100.0
        return value

    def _get_stability_score(self) -> float:
        last = self._load_last(STABILITY_LOG)
        if last is None:
            return 75.0
        return float(last.get("long_running_stability_score", 75.0))

    def _get_governance_score(self) -> float:
        last = self._load_last(GOVERNANCE_LOG)
        if last is None:
            return 75.0
        return float(last.get("runtime_governance_score", 75.0))

    def _get_watchdog_score(self) -> float:
        last = self._load_last(WATCHDOG_LOG)
        if last is None:
            return 75.0
        return float(last.get("watchdog_health_score", 75.0))

    def _get_recovery_success(self) -> float:
        last = self._load_last(RECOVERY_LOG)
        if last is None:
            return 75.0
        return float(last.get("recovery_success_rate", 75.0))

    def _get_guardian_state(self) -> str:
        last = self._load_last(GUARDIAN_LOG)
        if last is None:
            return "NORMAL"
        return str(last.get("guardian_state", "NORMAL"))

    def _get_active_critical_count(self) -> int:
        if not ACTIVE_INCIDENTS.exists():
            return 0
        try:
            with open(ACTIVE_INCIDENTS) as f:
                data = json.load(f)
            incidents = data if isinstance(data, list) else data.get("incidents", [])
            return sum(
                1 for i in incidents
                if str(i.get("severity", "")).upper() in ("CRITICAL", "EMERGENCY")
            )
        except Exception:
            return 0

    # ── Classification logic ───────────────────────────────────────────────────

    def _determine_classification(
        self,
        is_frozen: bool,
        uptime: float,
        incident_rate: float,
        replay: float,
        stability: float,
        governance: float,
        watchdog: float,
        recovery: float,
        active_critical: int,
        guardian_state: str,
        maturity: float,
    ) -> tuple[str, list[str]]:
        blocking: list[str] = []

        # FROZEN — hard override
        if is_frozen:
            reasons = []
            if active_critical > 0:
                reasons.append(f"{active_critical} CRITICAL/EMERGENCY incident(s) active")
            if guardian_state in ("FROZEN", "ROLLBACK"):
                reasons.append(f"guardian_state={guardian_state}")
            return "FROZEN", reasons

        # DEGRADED
        if stability < 40.0 or incident_rate >= 5.0:
            if stability < 40.0:
                blocking.append(f"stability={stability:.1f} < 40")
            if incident_rate >= 5.0:
                blocking.append(f"incident_rate={incident_rate:.2f}/h >= 5")
            return "DEGRADED", blocking

        # PRODUCTION_READY
        prod_blocks = []
        if uptime <= 72.0:
            prod_blocks.append(f"uptime={uptime:.1f}h <= 72h")
        if incident_rate >= 0.1:
            prod_blocks.append(f"incident_rate={incident_rate:.3f}/h >= 0.1/h")
        if replay <= 90.0:
            prod_blocks.append(f"replay_integrity={replay:.1f} <= 90")
        if stability <= 85.0:
            prod_blocks.append(f"stability={stability:.1f} <= 85")
        if governance <= 85.0:
            prod_blocks.append(f"governance={governance:.1f} <= 85")
        if watchdog <= 85.0:
            prod_blocks.append(f"watchdog={watchdog:.1f} <= 85")
        if recovery <= 90.0:
            prod_blocks.append(f"recovery={recovery:.1f} <= 90")
        if not prod_blocks:
            return "PRODUCTION_READY", []

        # MICRO_LIVE_READY
        micro_blocks = []
        if uptime <= 24.0:
            micro_blocks.append(f"uptime={uptime:.1f}h <= 24h")
        if incident_rate >= 0.5:
            micro_blocks.append(f"incident_rate={incident_rate:.3f}/h >= 0.5/h")
        if replay <= 80.0:
            micro_blocks.append(f"replay_integrity={replay:.1f} <= 80")
        if stability <= 75.0:
            micro_blocks.append(f"stability={stability:.1f} <= 75")
        if governance <= 75.0:
            micro_blocks.append(f"governance={governance:.1f} <= 75")
        if watchdog <= 75.0:
            micro_blocks.append(f"watchdog={watchdog:.1f} <= 75")
        if recovery <= 80.0:
            micro_blocks.append(f"recovery={recovery:.1f} <= 80")
        if not micro_blocks:
            return "MICRO_LIVE_READY", prod_blocks

        # PAPER_STABLE
        paper_blocks = []
        if uptime <= 4.0:
            paper_blocks.append(f"uptime={uptime:.1f}h <= 4h")
        if incident_rate >= 2.0:
            paper_blocks.append(f"incident_rate={incident_rate:.3f}/h >= 2/h")
        if stability <= 60.0:
            paper_blocks.append(f"stability={stability:.1f} <= 60")
        if governance <= 60.0:
            paper_blocks.append(f"governance={governance:.1f} <= 60")
        if watchdog <= 60.0:
            paper_blocks.append(f"watchdog={watchdog:.1f} <= 60")
        if not paper_blocks:
            return "PAPER_STABLE", micro_blocks

        # RESEARCH
        research_blocks = []
        if stability <= 40.0:
            research_blocks.append(f"stability={stability:.1f} <= 40")
        if not research_blocks:
            return "RESEARCH", paper_blocks

        # DEVELOPMENT (fallback — insufficient data or all checks fail)
        all_blocks = paper_blocks + research_blocks
        return "DEVELOPMENT", all_blocks

    # ── Confidence computation ─────────────────────────────────────────────────

    def _compute_confidence(
        self,
        maturity: float,
        classification: str,
        uptime: float,
        incident_rate: float,
        stability: float,
    ) -> float:
        """
        Start from operational_maturity_score.
        Reduce by 15 if on the classification boundary (within 5 pts of threshold).
        """
        confidence = maturity

        # Boundary penalties
        boundary_thresholds = {
            "PRODUCTION_READY": 85.0,
            "MICRO_LIVE_READY": 75.0,
            "PAPER_STABLE":     60.0,
            "RESEARCH":         45.0,
        }
        threshold = boundary_thresholds.get(classification)
        if threshold is not None and abs(maturity - threshold) <= 5.0:
            confidence -= 15.0

        # Slight boost for comfortable range
        if threshold is not None and maturity > threshold + 10.0:
            confidence = min(100.0, confidence + 5.0)

        return round(max(0.0, min(100.0, confidence)), 1)

    # ── Advancement requirements ───────────────────────────────────────────────

    def _compute_advancement(
        self,
        classification: str,
        uptime: float,
        incident_rate: float,
        replay: float,
        stability: float,
        governance: float,
        watchdog: float,
        recovery: float,
    ) -> list[str]:
        """Return what is needed to advance one classification level."""
        requirements: list[str] = []

        if classification == "DEVELOPMENT":
            requirements = [
                "Establish uptime > 4h",
                "Bring stability_score > 40",
                f"Current stability={stability:.1f}",
            ]
        elif classification == "RESEARCH":
            reqs = []
            if uptime <= 4.0:
                reqs.append(f"uptime > 4h (currently {uptime:.1f}h)")
            if incident_rate >= 2.0:
                reqs.append(f"incident_rate < 2/h (currently {incident_rate:.2f}/h)")
            if stability <= 60.0:
                reqs.append(f"stability > 60 (currently {stability:.1f})")
            if governance <= 60.0:
                reqs.append(f"governance > 60 (currently {governance:.1f})")
            if watchdog <= 60.0:
                reqs.append(f"watchdog > 60 (currently {watchdog:.1f})")
            requirements = reqs or ["Maintain current metrics for PAPER_STABLE"]
        elif classification == "PAPER_STABLE":
            reqs = []
            if uptime <= 24.0:
                reqs.append(f"uptime > 24h (currently {uptime:.1f}h)")
            if incident_rate >= 0.5:
                reqs.append(f"incident_rate < 0.5/h (currently {incident_rate:.3f}/h)")
            if replay <= 80.0:
                reqs.append(f"replay_integrity > 80 (currently {replay:.1f})")
            if stability <= 75.0:
                reqs.append(f"stability > 75 (currently {stability:.1f})")
            if governance <= 75.0:
                reqs.append(f"governance > 75 (currently {governance:.1f})")
            if watchdog <= 75.0:
                reqs.append(f"watchdog > 75 (currently {watchdog:.1f})")
            if recovery <= 80.0:
                reqs.append(f"recovery > 80 (currently {recovery:.1f})")
            requirements = reqs or ["Maintain current metrics for MICRO_LIVE_READY"]
        elif classification == "MICRO_LIVE_READY":
            reqs = []
            if uptime <= 72.0:
                reqs.append(f"uptime > 72h (currently {uptime:.1f}h)")
            if incident_rate >= 0.1:
                reqs.append(f"incident_rate < 0.1/h (currently {incident_rate:.4f}/h)")
            if replay <= 90.0:
                reqs.append(f"replay_integrity > 90 (currently {replay:.1f})")
            if stability <= 85.0:
                reqs.append(f"stability > 85 (currently {stability:.1f})")
            if governance <= 85.0:
                reqs.append(f"governance > 85 (currently {governance:.1f})")
            if watchdog <= 85.0:
                reqs.append(f"watchdog > 85 (currently {watchdog:.1f})")
            if recovery <= 90.0:
                reqs.append(f"recovery > 90 (currently {recovery:.1f})")
            requirements = reqs or ["All requirements met for PRODUCTION_READY"]
        elif classification in ("PRODUCTION_READY",):
            requirements = ["Already at highest classification"]
        elif classification == "FROZEN":
            requirements = [
                "Resolve all CRITICAL/EMERGENCY incidents",
                "Restore guardian to NORMAL/MONITORING state",
            ]
        elif classification == "DEGRADED":
            requirements = [
                f"Bring stability_score > 40 (currently {stability:.1f})",
                f"Reduce incident_rate < 5/h (currently {incident_rate:.2f}/h)",
            ]

        return requirements

    # ── Classification history ─────────────────────────────────────────────────

    def _load_classification_history(self, n: int = 5) -> list[str]:
        if not self.readiness_log.exists():
            return []
        records: list[str] = []
        try:
            with open(self.readiness_log) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            cls = record.get("classification", "")
                            if cls:
                                records.append(cls)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return records[-n:]

    # ── Recommendation ─────────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        classification: str,
        confidence: float,
        blocking: list[str],
        advancement: list[str],
    ) -> str:
        if classification == "FROZEN":
            return (
                f"SYSTEM FROZEN ({confidence:.0f}% confidence): "
                f"{'; '.join(blocking)}. "
                "All operations halted. Resolve incidents first."
            )
        if classification == "CRITICAL":
            return (
                f"CRITICAL state — no normal operations allowed. "
                f"Resolve: {'; '.join(blocking[:3])}"
            )
        if classification == "DEGRADED":
            return (
                f"System DEGRADED ({confidence:.0f}% confidence): "
                f"{'; '.join(blocking)}. "
                "Stabilize before advancing."
            )
        if classification == "DEVELOPMENT":
            return (
                f"DEVELOPMENT stage ({confidence:.0f}% confidence). "
                "Insufficient operational data. "
                f"Next step: {advancement[0] if advancement else 'collect more data'}"
            )
        if classification == "RESEARCH":
            return (
                f"RESEARCH mode ({confidence:.0f}% confidence). "
                "Suitable for backtesting and analysis only. "
                f"To advance: {advancement[0] if advancement else 'see advancement_requirements'}"
            )
        if classification == "PAPER_STABLE":
            return (
                f"PAPER_STABLE ({confidence:.0f}% confidence). "
                "Paper trading allowed. No live execution. "
                f"To advance: {advancement[0] if advancement else 'maintain metrics'}"
            )
        if classification == "MICRO_LIVE_READY":
            return (
                f"MICRO_LIVE_READY ({confidence:.0f}% confidence). "
                "Micro live execution permitted under guardian supervision. "
                f"To reach PRODUCTION_READY: {advancement[0] if advancement else 'maintain metrics'}"
            )
        if classification == "PRODUCTION_READY":
            return (
                f"PRODUCTION_READY ({confidence:.0f}% confidence). "
                "All operational thresholds met. Full live execution framework approved."
            )
        return f"Classification={classification} confidence={confidence:.0f}%"

    # ── Persistence ────────────────────────────────────────────────────────────

    def _persist(self, report: ProductionReadinessReport) -> None:
        try:
            self.readiness_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":             report.evaluated_at,
                "report_id":                report.report_id,
                "classification":           report.classification,
                "readiness_confidence":     report.readiness_confidence,
                "operational_maturity_score": report.operational_maturity_score,
                "dimensions_passed":        report.dimensions_passed,
                "dimensions_total":         report.dimensions_total,
                "uptime_hours":             report.uptime_hours,
                "incident_rate_per_hour":   report.incident_rate_per_hour,
                "blocking_factors":         report.blocking_factors,
            }
            with open(self.readiness_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _load_last(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        last: dict | None = None
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return last


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Production Readiness Classifier — Phase R R-9"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    classifier = ProductionReadinessClassifier()
    report = classifier.classify()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    cls_icons = {
        "PRODUCTION_READY": "**",
        "MICRO_LIVE_READY": " *",
        "PAPER_STABLE":     " ~",
        "RESEARCH":         " r",
        "DEGRADED":         " !",
        "FROZEN":           " X",
        "DEVELOPMENT":      " d",
    }
    icon = cls_icons.get(report.classification, " ?")

    print(f"\nProduction Readiness Classifier — Phase R R-9")
    print(f"  report_id:               {report.report_id}")
    print(f"  classification:          [{icon}] {report.classification}")
    print(f"  readiness_confidence:    {report.readiness_confidence:.1f}%")
    print(f"  operational_maturity:    {report.operational_maturity_score:.1f}/100")
    print(f"  uptime_hours:            {report.uptime_hours:.2f}h")
    print(f"  incident_rate_per_hour:  {report.incident_rate_per_hour:.4f}/h")
    print(f"  dimensions:              {report.dimensions_passed}/{report.dimensions_total} passed")
    print()
    for dim in report.dimensions:
        status = "+" if dim.passed else "-"
        print(
            f"  [{status}] {dim.name:<30} "
            f"value={dim.value:>7.2f}  threshold={dim.threshold:>7.2f}"
        )
    if report.blocking_factors:
        print(f"\n  Blocking factors:")
        for bf in report.blocking_factors:
            print(f"    - {bf}")
    if report.advancement_requirements:
        print(f"\n  To advance one level:")
        for req in report.advancement_requirements:
            print(f"    -> {req}")
    if report.classification_history:
        print(f"\n  Classification history (last {len(report.classification_history)}):")
        print(f"    {' -> '.join(report.classification_history)}")
    print(f"\n  -> {report.recommendation}")


if __name__ == "__main__":
    main()

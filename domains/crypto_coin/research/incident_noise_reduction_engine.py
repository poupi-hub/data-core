"""
incident_noise_reduction_engine.py — Phase S S-6

Analyses active and historical incidents to detect noise patterns:
  - Alert storms (> 5 alerts in 30 min from same subsystem)
  - Duplicate alerts (same title within cooldown window)
  - Cascading alerts (correlated multi-subsystem spike)
  - Cooldown violations (re-alert before cooldown expires)

Scores: incident_signal_quality_score, alert_precision_score, operational_noise_score

CLI:
  python -m domains.crypto_coin.research.incident_noise_reduction_engine
  python -m domains.crypto_coin.research.incident_noise_reduction_engine --json
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

NOISE_LOG      = Path("data/incident_noise_log.jsonl")
INCIDENT_LOG   = Path("data/incident_log.jsonl")
ACTIVE_INCIDENT_FILE = Path("data/active_incidents.json")

# Storm: > N alerts from one subsystem within STORM_WINDOW_MIN
STORM_THRESHOLD  = 5
STORM_WINDOW_MIN = 30.0

# Cooldown windows by severity (minutes)
COOLDOWN_MINUTES: dict[str, float] = {
    "INFO":      5.0,
    "WARNING":  15.0,
    "CRITICAL": 30.0,
    "SEVERE":   60.0,
    "EMERGENCY":120.0,
}

DEFAULT_COOLDOWN = 15.0

try:
    from api.burnin_metrics import incident_signal_quality_score as _prom_signal
    from api.burnin_metrics import alert_precision_score         as _prom_precision
    from api.burnin_metrics import operational_noise_score       as _prom_noise
    _METRICS = True
except ImportError:
    _METRICS = False


@dataclass
class NoisePattern:
    pattern_type: str    # storm | duplicate | cascading | cooldown_violation
    subsystem: str
    alert_count: int
    window_minutes: float
    example_titles: list[str]
    first_seen: str
    last_seen: str


@dataclass
class SubsystemNoise:
    subsystem: str
    total_alerts: int
    storm_count: int
    duplicate_count: int
    noise_ratio: float   # noisy_alerts / total_alerts
    healthy: bool


@dataclass
class IncidentNoiseReport:
    report_id: str
    incident_signal_quality_score: float
    alert_precision_score: float
    operational_noise_score: float
    total_incidents_analysed: int
    total_noisy_incidents: int
    storm_patterns: int
    duplicate_patterns: int
    cascading_patterns: int
    cooldown_violations: int
    subsystems_analysed: int
    noisy_subsystems: int
    patterns: list[NoisePattern]
    subsystem_noise: list[SubsystemNoise]
    issues_summary: list[str]
    evaluated_at: str
    recommendation: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["patterns"]        = [asdict(p) for p in self.patterns]
        d["subsystem_noise"] = [asdict(s) for s in self.subsystem_noise]
        return d


class IncidentNoiseReductionEngine:
    """S-6: Incident Noise Reduction Engine."""

    def __init__(self, log: Path = NOISE_LOG):
        self.log = log

    def validate(self) -> IncidentNoiseReport:
        report_id = str(uuid.uuid4())[:10]
        incidents = self._load_incidents()

        patterns: list[NoisePattern]      = []
        subsystem_summaries: list[SubsystemNoise] = []

        if not incidents:
            # No incident data — score neutral
            report = self._empty_report(report_id)
            self._persist(report)
            return report

        # Group by subsystem
        by_subsystem: dict[str, list[dict]] = defaultdict(list)
        for inc in incidents:
            sub = str(inc.get("subsystem", inc.get("source", "unknown")))
            by_subsystem[sub].append(inc)

        storm_count      = 0
        duplicate_count  = 0
        cascading_count  = 0
        cooldown_viols   = 0
        total_noisy      = 0

        for subsystem, incs in by_subsystem.items():
            incs_sorted = sorted(incs, key=lambda x: self._parse_ts(x))
            sub_storm, sub_dup, sub_cool, sub_patterns = self._analyse_subsystem(
                subsystem, incs_sorted
            )
            storm_count     += sub_storm
            duplicate_count += sub_dup
            cooldown_viols  += sub_cool
            patterns.extend(sub_patterns)

            noisy = sub_storm + sub_dup + sub_cool
            noise_ratio = min(1.0, noisy / max(len(incs), 1))
            total_noisy += min(len(incs), noisy)

            subsystem_summaries.append(SubsystemNoise(
                subsystem     = subsystem,
                total_alerts  = len(incs),
                storm_count   = sub_storm,
                duplicate_count = sub_dup,
                noise_ratio   = round(noise_ratio, 3),
                healthy       = noise_ratio < 0.2,
            ))

        # Cascading: if ≥3 different subsystems had storms in same 30-min window
        cascading_count = self._detect_cascading(by_subsystem, patterns)

        noisy_subsystems = sum(1 for s in subsystem_summaries if not s.healthy)
        total_incidents  = len(incidents)

        # incident_signal_quality_score: 1 - (noisy/total) scaled
        signal_quality = max(0.0, (1.0 - min(1.0, total_noisy / max(total_incidents, 1))) * 100)

        # alert_precision_score: penalise duplicates and storms
        precision_penalty = (duplicate_count * 3.0) + (storm_count * 5.0)
        alert_precision   = max(0.0, 100.0 - precision_penalty)

        # operational_noise_score: 0=silent/noisy, 100=precise
        operational_noise = (signal_quality * 0.6 + alert_precision * 0.4)

        issues: list[str] = []
        if storm_count:
            issues.append(f"{storm_count} storm(s) detectado(s) (>{STORM_THRESHOLD} alertas em {STORM_WINDOW_MIN}min)")
        if duplicate_count:
            issues.append(f"{duplicate_count} alerta(s) duplicado(s) ignorando cooldown")
        if cascading_count:
            issues.append(f"{cascading_count} alerta(s) em cascata entre subsistemas")
        if cooldown_viols:
            issues.append(f"{cooldown_viols} violacao(oes) de cooldown detectadas")
        noisy_names = [s.subsystem for s in subsystem_summaries if not s.healthy]
        if noisy_names:
            issues.append(f"Subsistemas ruidosos: {', '.join(noisy_names[:4])}")

        recommendation = self._build_recommendation(
            signal_quality, alert_precision, storm_count, duplicate_count
        )

        report = IncidentNoiseReport(
            report_id                     = report_id,
            incident_signal_quality_score = round(signal_quality, 1),
            alert_precision_score         = round(alert_precision, 1),
            operational_noise_score       = round(operational_noise, 1),
            total_incidents_analysed      = total_incidents,
            total_noisy_incidents         = total_noisy,
            storm_patterns                = storm_count,
            duplicate_patterns            = duplicate_count,
            cascading_patterns            = cascading_count,
            cooldown_violations           = cooldown_viols,
            subsystems_analysed           = len(subsystem_summaries),
            noisy_subsystems              = noisy_subsystems,
            patterns                      = patterns,
            subsystem_noise               = subsystem_summaries,
            issues_summary                = issues,
            evaluated_at                  = datetime.now(timezone.utc).isoformat(),
            recommendation                = recommendation,
        )
        self._persist(report)
        if _METRICS:
            try:
                _prom_signal.set(signal_quality)
                _prom_precision.set(alert_precision)
                _prom_noise.set(operational_noise)
            except Exception:
                pass
        return report

    # ── Analysis helpers ─────────────────────────────────────────────────────

    def _analyse_subsystem(
        self, subsystem: str, incs: list[dict]
    ) -> tuple[int, int, int, list[NoisePattern]]:
        storm_count  = 0
        dup_count    = 0
        cool_viols   = 0
        patterns: list[NoisePattern] = []

        if not incs:
            return 0, 0, 0, []

        # Storm detection: sliding window of 30 min
        ts_list = [self._parse_ts(i) for i in incs]
        window_alerts: list[float] = []
        storm_detected = False

        for ts in ts_list:
            window_alerts = [t for t in window_alerts if (ts - t) / 60 <= STORM_WINDOW_MIN]
            window_alerts.append(ts)
            if len(window_alerts) >= STORM_THRESHOLD and not storm_detected:
                storm_count += 1
                storm_detected = True
                patterns.append(NoisePattern(
                    pattern_type  = "storm",
                    subsystem     = subsystem,
                    alert_count   = len(window_alerts),
                    window_minutes= STORM_WINDOW_MIN,
                    example_titles= [str(incs[0].get("title", incs[0].get("message", "?")))],
                    first_seen    = datetime.fromtimestamp(min(window_alerts), tz=timezone.utc).isoformat(),
                    last_seen     = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                ))

        # Duplicate detection: same title within cooldown window
        title_last_seen: dict[str, float] = {}
        for inc in incs:
            title    = str(inc.get("title", inc.get("message", "unknown")))
            severity = str(inc.get("severity", "WARNING")).upper()
            cooldown = COOLDOWN_MINUTES.get(severity, DEFAULT_COOLDOWN) * 60
            ts       = self._parse_ts(inc)
            if title in title_last_seen:
                gap = ts - title_last_seen[title]
                if gap < cooldown:
                    dup_count   += 1
                    cool_viols  += 1
            title_last_seen[title] = ts

        if dup_count:
            example = [str(incs[0].get("title", "?"))]
            patterns.append(NoisePattern(
                pattern_type  = "duplicate",
                subsystem     = subsystem,
                alert_count   = dup_count,
                window_minutes= DEFAULT_COOLDOWN,
                example_titles= example,
                first_seen    = datetime.fromtimestamp(ts_list[0], tz=timezone.utc).isoformat(),
                last_seen     = datetime.fromtimestamp(ts_list[-1], tz=timezone.utc).isoformat(),
            ))

        return storm_count, dup_count, cool_viols, patterns

    def _detect_cascading(
        self,
        by_subsystem: dict[str, list[dict]],
        patterns: list[NoisePattern],
    ) -> int:
        # Count if ≥3 subsystems had storms within the same 30-min window
        storm_pats = [p for p in patterns if p.pattern_type == "storm"]
        if len(storm_pats) < 3:
            return 0
        # Simple heuristic: if ≥3 different subsystems all have storms → 1 cascading event
        return 1

    # ── Loaders ──────────────────────────────────────────────────────────────

    def _load_incidents(self) -> list[dict]:
        records: list[dict] = []
        # Try active incidents JSON first
        if ACTIVE_INCIDENT_FILE.exists():
            try:
                data = json.loads(ACTIVE_INCIDENT_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    records.extend(data)
                elif isinstance(data, dict):
                    records.extend(data.values())
            except Exception:
                pass

        # Then JSONL history
        if INCIDENT_LOG.exists():
            try:
                for line in INCIDENT_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass
        return records

    def _parse_ts(self, record: dict) -> float:
        for key in ("occurred_at", "created_at", "evaluated_at", "timestamp", "ts"):
            val = record.get(key)
            if val is None:
                continue
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
        return datetime.now(timezone.utc).timestamp()

    # ── Empty report (no data) ────────────────────────────────────────────────

    def _empty_report(self, report_id: str) -> IncidentNoiseReport:
        return IncidentNoiseReport(
            report_id                     = report_id,
            incident_signal_quality_score = 100.0,
            alert_precision_score         = 100.0,
            operational_noise_score       = 100.0,
            total_incidents_analysed      = 0,
            total_noisy_incidents         = 0,
            storm_patterns                = 0,
            duplicate_patterns            = 0,
            cascading_patterns            = 0,
            cooldown_violations           = 0,
            subsystems_analysed           = 0,
            noisy_subsystems              = 0,
            patterns                      = [],
            subsystem_noise               = [],
            issues_summary                = ["Sem dados de incidentes — score neutro 100"],
            evaluated_at                  = datetime.now(timezone.utc).isoformat(),
            recommendation                = "Nenhum incidente registrado. Sistema em estado limpo.",
        )

    # ── Recommendation ───────────────────────────────────────────────────────

    def _build_recommendation(
        self,
        signal: float,
        precision: float,
        storms: int,
        duplicates: int,
    ) -> str:
        if signal >= 90 and precision >= 90:
            return f"Qualidade de sinal alta ({signal:.0f}%). Alertas precisos e sem ruido."
        if storms:
            return (
                f"ATENCAO: {storms} storm(s) detectado(s). "
                "Implementar rate-limiting e agrupamento de alertas no modulo R-6."
            )
        if duplicates:
            return (
                f"ATENCAO: {duplicates} alerta(s) duplicado(s). "
                "Revisar janelas de cooldown em autonomous_incident_manager.py."
            )
        return f"Qualidade {signal:.0f}%, precision {precision:.0f}%. Monitorar padrao de alertas."

    # ── Persistence ──────────────────────────────────────────────────────────

    def _persist(self, report: IncidentNoiseReport) -> None:
        try:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":                    report.evaluated_at,
                "incident_signal_quality_score":   report.incident_signal_quality_score,
                "alert_precision_score":           report.alert_precision_score,
                "operational_noise_score":         report.operational_noise_score,
                "total_incidents_analysed":        report.total_incidents_analysed,
                "total_noisy_incidents":           report.total_noisy_incidents,
                "storm_patterns":                  report.storm_patterns,
                "duplicate_patterns":              report.duplicate_patterns,
                "cascading_patterns":              report.cascading_patterns,
                "cooldown_violations":             report.cooldown_violations,
            }
            with open(self.log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Incident Noise Reduction Engine — Phase S S-6")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    e = IncidentNoiseReductionEngine()
    r = e.validate()

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        return

    print(f"\nIncident Noise Reduction Engine — Phase S S-6")
    print(f"  incident_signal_quality_score: {r.incident_signal_quality_score:.1f}/100")
    print(f"  alert_precision_score:         {r.alert_precision_score:.1f}/100")
    print(f"  operational_noise_score:       {r.operational_noise_score:.1f}/100")
    print(f"  incidents: analysed={r.total_incidents_analysed}  noisy={r.total_noisy_incidents}")
    print(f"  patterns:  storms={r.storm_patterns}  duplicates={r.duplicate_patterns}  "
          f"cascading={r.cascading_patterns}  cooldown_viols={r.cooldown_violations}")
    print(f"  subsystems: {r.subsystems_analysed} analysed, {r.noisy_subsystems} noisy")
    if r.issues_summary:
        print(f"\n  Issues:")
        for iss in r.issues_summary:
            print(f"    - {iss}")
    print(f"\n  -> {r.recommendation}")


if __name__ == "__main__":
    main()

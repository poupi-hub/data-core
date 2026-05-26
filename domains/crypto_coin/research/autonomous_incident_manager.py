"""
autonomous_incident_manager.py — Phase R R-6

Structured Incident Management System.

Cria, gerencia e persiste incidentes operacionais estruturados com
ciclo de vida completo (OPEN → IN_PROGRESS → RESOLVED / SUPPRESSED).

Scores produzidos:
  - incident_severity_score:  ponderado por severidade dos incidentes ativos (0-100)
  - incident_frequency_score: taxa de incidentes na ultima hora (0-100)
  - operational_risk_score:   risco operacional combinado (0-100)

Severity levels:
  INFO=1, WARNING=2, DEGRADED=3, CRITICAL=4, EMERGENCY=5

TTL por severidade (auto-resolucao):
  INFO=30min, WARNING=60min, DEGRADED=120min, CRITICAL=0 (manual), EMERGENCY=0 (manual)

CLI:
  python -m domains.crypto_coin.research.autonomous_incident_manager --summary [--json]
  python -m domains.crypto_coin.research.autonomous_incident_manager --create \\
      --subsystem api --severity WARNING --root-cause "API latency elevated"
  python -m domains.crypto_coin.research.autonomous_incident_manager --resolve INCIDENT_ID
  python -m domains.crypto_coin.research.autonomous_incident_manager --auto-resolve
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

INCIDENT_LOG          = Path("data/incident_log.jsonl")
ACTIVE_INCIDENTS_PATH = Path("data/active_incidents.json")

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        incident_severity_score    as _prom_severity,
        incident_frequency_score   as _prom_frequency,
        operational_risk_score     as _prom_op_risk,
        incident_count_total       as _prom_count,
        critical_incidents_total   as _prom_critical,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# ── Severity constants ──────────────────────────────────────────────────────────

SEVERITY_LEVELS: dict[str, int] = {
    "INFO":      1,
    "WARNING":   2,
    "DEGRADED":  3,
    "CRITICAL":  4,
    "EMERGENCY": 5,
}

# TTL em minutos (0 = sem auto-resolucao)
SEVERITY_TTL: dict[str, int] = {
    "INFO":      30,
    "WARNING":   60,
    "DEGRADED":  120,
    "CRITICAL":  0,
    "EMERGENCY": 0,
}

VALID_SEVERITIES  = set(SEVERITY_LEVELS.keys())
VALID_STATUSES    = {"OPEN", "IN_PROGRESS", "RESOLVED", "SUPPRESSED"}

# Normalizacao do severity_score: max pontos possiveis para ~10 incidentes EMERGENCY
_MAX_SEVERITY_POINTS = 50 * 5   # 10 incidentes * 5 * 5 pontos cada


# ── Data classes ─────────────────────────────────────────────────────────────────

@dataclass
class Incident:
    """Incidente operacional estruturado."""
    incident_id:           str
    subsystem:             str
    severity:              str       # INFO | WARNING | DEGRADED | CRITICAL | EMERGENCY
    severity_level:        int       # 1-5
    root_cause:            str
    trigger_metrics:       dict[str, float]
    correlated_metrics:    dict[str, float]
    opened_at:             str
    updated_at:            str
    resolved_at:           str | None
    suggested_recovery:    str
    rollback_recommendation: bool
    recovery_status:       str       # OPEN | IN_PROGRESS | RESOLVED | SUPPRESSED
    resolution_note:       str | None
    ttl_minutes:           int       # 0 = sem auto-resolucao

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Incident":
        return cls(
            incident_id            = d["incident_id"],
            subsystem              = d["subsystem"],
            severity               = d["severity"],
            severity_level         = d["severity_level"],
            root_cause             = d["root_cause"],
            trigger_metrics        = d.get("trigger_metrics", {}),
            correlated_metrics     = d.get("correlated_metrics", {}),
            opened_at              = d["opened_at"],
            updated_at             = d["updated_at"],
            resolved_at            = d.get("resolved_at"),
            suggested_recovery     = d.get("suggested_recovery", ""),
            rollback_recommendation= d.get("rollback_recommendation", False),
            recovery_status        = d.get("recovery_status", "OPEN"),
            resolution_note        = d.get("resolution_note"),
            ttl_minutes            = d.get("ttl_minutes", 60),
        )


@dataclass
class IncidentSummary:
    """Sumario do estado de incidentes operacionais."""
    report_id:               str
    incident_severity_score: float   # 0-100 (100 = pior)
    incident_frequency_score: float  # 0-100
    operational_risk_score:  float   # 0-100
    active_incidents:        list[Incident]
    active_count:            int
    open_critical:           int
    open_emergency:          int
    resolved_last_hour:      int
    highest_severity:        str
    risk_assessment:         str
    evaluated_at:            str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["active_incidents"] = [asdict(inc) for inc in self.active_incidents]
        return d


# ── Manager ──────────────────────────────────────────────────────────────────────

class AutonomousIncidentManager:
    """
    R-6: Sistema estruturado de gerenciamento de incidentes.

    Ciclo de vida: OPEN -> IN_PROGRESS -> RESOLVED | SUPPRESSED
    Auto-resolucao por TTL para severidades INFO/WARNING/DEGRADED.
    """

    def __init__(
        self,
        incident_log:          Path = INCIDENT_LOG,
        active_incidents_path: Path = ACTIVE_INCIDENTS_PATH,
    ):
        self.incident_log          = incident_log
        self.active_incidents_path = active_incidents_path

    # ── Public API ───────────────────────────────────────────────────────────────

    def create_incident(
        self,
        subsystem:               str,
        severity:                str,
        root_cause:              str,
        trigger_metrics:         dict[str, float] | None = None,
        correlated_metrics:      dict[str, float] | None = None,
        suggested_recovery:      str = "",
        rollback_recommendation: bool = False,
    ) -> Incident:
        """Cria e persiste um novo incidente."""
        severity = severity.upper()
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"Severidade invalida: {severity!r}. Validas: {sorted(VALID_SEVERITIES)}")

        now_str = datetime.now(timezone.utc).isoformat()
        incident = Incident(
            incident_id            = f"INC-{str(uuid.uuid4())[:8].upper()}",
            subsystem              = subsystem,
            severity               = severity,
            severity_level         = SEVERITY_LEVELS[severity],
            root_cause             = root_cause,
            trigger_metrics        = trigger_metrics or {},
            correlated_metrics     = correlated_metrics or {},
            opened_at              = now_str,
            updated_at             = now_str,
            resolved_at            = None,
            suggested_recovery     = suggested_recovery,
            rollback_recommendation= rollback_recommendation,
            recovery_status        = "OPEN",
            resolution_note        = None,
            ttl_minutes            = SEVERITY_TTL[severity],
        )

        # Atualiza active incidents
        actives = self._load_active_incidents()
        actives.append(incident)
        self._save_active_incidents(actives)

        # Persiste no log
        self._append_log(incident.to_dict())

        # Metricas
        self._push_counter(severity)

        return incident

    def resolve_incident(self, incident_id: str, resolution_note: str = "") -> bool:
        """Resolve um incidente pelo ID. Retorna True se encontrado."""
        actives = self._load_active_incidents()
        found = False
        now_str = datetime.now(timezone.utc).isoformat()
        for inc in actives:
            if inc.incident_id == incident_id:
                inc.recovery_status  = "RESOLVED"
                inc.resolved_at      = now_str
                inc.updated_at       = now_str
                inc.resolution_note  = resolution_note or "Resolvido manualmente"
                self._append_log(inc.to_dict())
                found = True
                break
        if found:
            remaining = [i for i in actives if i.incident_id != incident_id]
            self._save_active_incidents(remaining)
        return found

    def suppress_incident(self, incident_id: str) -> bool:
        """Suprime um incidente pelo ID. Retorna True se encontrado."""
        actives = self._load_active_incidents()
        found = False
        now_str = datetime.now(timezone.utc).isoformat()
        for inc in actives:
            if inc.incident_id == incident_id:
                inc.recovery_status = "SUPPRESSED"
                inc.updated_at      = now_str
                self._append_log(inc.to_dict())
                found = True
                break
        if found:
            remaining = [i for i in actives if i.incident_id != incident_id]
            self._save_active_incidents(remaining)
        return found

    def get_active_incidents(self) -> list[Incident]:
        """Retorna lista de incidentes ativos (OPEN ou IN_PROGRESS)."""
        return self._load_active_incidents()

    def get_summary(self) -> IncidentSummary:
        """Computa e retorna sumario do estado de incidentes."""
        report_id = str(uuid.uuid4())[:10]
        actives   = self._load_active_incidents()
        now_dt    = datetime.now(timezone.utc)

        # Severity score
        severity_points = sum(inc.severity_level * 5 for inc in actives)
        incident_severity_score = min(100.0, (severity_points / max(_MAX_SEVERITY_POINTS, 1)) * 100.0)
        incident_severity_score = round(incident_severity_score, 1)

        # Frequency score: incidentes criados na ultima hora
        one_hour_ago = now_dt - timedelta(hours=1)
        recent_count = sum(
            1 for inc in actives
            if self._parse_dt(inc.opened_at) >= one_hour_ago
        )
        # tambem conta do log completo (incidentes ja resolvidos na ultima hora)
        recent_count += self._count_log_entries_since(one_hour_ago)
        incident_frequency_score = round(min(100.0, (recent_count / 10.0) * 100.0), 1)

        operational_risk_score = round(
            incident_severity_score * 0.6 + incident_frequency_score * 0.4, 1
        )

        open_critical  = sum(1 for i in actives if i.severity in ("CRITICAL", "EMERGENCY") and i.recovery_status == "OPEN")
        open_emergency = sum(1 for i in actives if i.severity == "EMERGENCY")
        resolved_last_hour = self._count_resolved_since(one_hour_ago)

        highest_sev = "NONE"
        if actives:
            highest_sev = max(actives, key=lambda i: i.severity_level).severity

        risk_assessment = self._build_risk_assessment(
            operational_risk_score, actives, open_critical, open_emergency,
        )

        summary = IncidentSummary(
            report_id                = report_id,
            incident_severity_score  = incident_severity_score,
            incident_frequency_score = incident_frequency_score,
            operational_risk_score   = operational_risk_score,
            active_incidents         = actives,
            active_count             = len(actives),
            open_critical            = open_critical,
            open_emergency           = open_emergency,
            resolved_last_hour       = resolved_last_hour,
            highest_severity         = highest_sev,
            risk_assessment          = risk_assessment,
            evaluated_at             = now_dt.isoformat(),
        )

        self._push_summary_metrics(summary)
        return summary

    def auto_resolve_expired(self) -> int:
        """Auto-resolve incidentes que excederam seu TTL. Retorna quantidade resolvida."""
        actives  = self._load_active_incidents()
        now_dt   = datetime.now(timezone.utc)
        resolved = 0
        remaining: list[Incident] = []

        for inc in actives:
            if inc.ttl_minutes <= 0:
                remaining.append(inc)
                continue
            opened_dt = self._parse_dt(inc.opened_at)
            age_min   = (now_dt - opened_dt).total_seconds() / 60.0
            if age_min >= inc.ttl_minutes:
                inc.recovery_status = "RESOLVED"
                inc.resolved_at     = now_dt.isoformat()
                inc.updated_at      = now_dt.isoformat()
                inc.resolution_note = f"Auto-resolvido por TTL ({inc.ttl_minutes} min)"
                self._append_log(inc.to_dict())
                resolved += 1
            else:
                remaining.append(inc)

        if resolved > 0:
            self._save_active_incidents(remaining)

        return resolved

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _load_active_incidents(self) -> list[Incident]:
        """Le incidentes ativos do arquivo JSON."""
        if not self.active_incidents_path.exists():
            return []
        try:
            with open(self.active_incidents_path) as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            return [Incident.from_dict(d) for d in data]
        except Exception:
            return []

    def _save_active_incidents(self, incidents: list[Incident]) -> None:
        """Persiste lista de incidentes ativos."""
        try:
            self.active_incidents_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.active_incidents_path, "w") as f:
                json.dump([inc.to_dict() for inc in incidents], f, indent=2)
        except Exception:
            pass

    def _append_log(self, entry: dict) -> None:
        """Adiciona entrada ao JSONL de incidentes."""
        try:
            self.incident_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.incident_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ── Analytics helpers ────────────────────────────────────────────────────────

    def _count_log_entries_since(self, since: datetime) -> int:
        """Conta entradas no log desde 'since' com recovery_status OPEN."""
        if not self.incident_log.exists():
            return 0
        count = 0
        try:
            with open(self.incident_log) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # so conta aberturas, nao updates
                        if entry.get("recovery_status") != "OPEN":
                            continue
                        opened = self._parse_dt(entry.get("opened_at", ""))
                        if opened >= since:
                            count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return count

    def _count_resolved_since(self, since: datetime) -> int:
        """Conta incidentes resolvidos desde 'since'."""
        if not self.incident_log.exists():
            return 0
        count = 0
        try:
            with open(self.incident_log) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("recovery_status") not in ("RESOLVED", "SUPPRESSED"):
                            continue
                        resolved_str = entry.get("resolved_at")
                        if not resolved_str:
                            continue
                        resolved_dt = self._parse_dt(resolved_str)
                        if resolved_dt >= since:
                            count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return count

    def _parse_dt(self, ts: str) -> datetime:
        """Faz parse seguro de timestamp ISO para datetime com tz."""
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    def _build_risk_assessment(
        self,
        risk_score:    float,
        actives:       list[Incident],
        open_critical: int,
        open_emergency: int,
    ) -> str:
        if open_emergency > 0:
            return (
                f"EMERGENCIA: {open_emergency} incidente(s) EMERGENCY ativos. "
                "Intervencao imediata necessaria."
            )
        if open_critical > 0:
            return (
                f"CRITICO: {open_critical} incidente(s) CRITICAL abertos. "
                "Requer atencao urgente."
            )
        if risk_score >= 60:
            return (
                f"RISCO ALTO ({risk_score:.0f}/100): {len(actives)} incidente(s) ativos. "
                "Monitoramento intensivo."
            )
        if risk_score >= 30:
            return (
                f"RISCO MODERADO ({risk_score:.0f}/100): {len(actives)} incidente(s) ativos. "
                "Monitorar evolucao."
            )
        if actives:
            return f"RISCO BAIXO ({risk_score:.0f}/100): {len(actives)} incidente(s) menor(es) ativos."
        return f"OPERACIONAL ({risk_score:.0f}/100): Nenhum incidente ativo."

    # ── Metrics ──────────────────────────────────────────────────────────────────

    def _push_counter(self, severity: str) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_count.labels(severity=severity).inc()
            if severity in ("CRITICAL", "EMERGENCY"):
                _prom_critical.inc()
        except Exception:
            pass

    def _push_summary_metrics(self, summary: IncidentSummary) -> None:
        if not _METRICS_AVAILABLE:
            return
        try:
            _prom_severity.set(summary.incident_severity_score)
            _prom_frequency.set(summary.incident_frequency_score)
            _prom_op_risk.set(summary.operational_risk_score)
        except Exception:
            pass


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Incident Manager — Phase R R-6"
    )
    parser.add_argument("--summary",      action="store_true", help="Exibir sumario de incidentes")
    parser.add_argument("--create",       action="store_true", help="Criar novo incidente")
    parser.add_argument("--resolve",      metavar="INCIDENT_ID", help="Resolver incidente por ID")
    parser.add_argument("--suppress",     metavar="INCIDENT_ID", help="Suprimir incidente por ID")
    parser.add_argument("--auto-resolve", action="store_true", help="Auto-resolver incidentes expirados")
    parser.add_argument("--list",         action="store_true", help="Listar incidentes ativos")
    parser.add_argument("--json",         action="store_true", help="Saida JSON")

    # Parametros para --create
    parser.add_argument("--subsystem",    default="unknown",   help="Subsistema afetado")
    parser.add_argument("--severity",     default="WARNING",   help="INFO|WARNING|DEGRADED|CRITICAL|EMERGENCY")
    parser.add_argument("--root-cause",   default="",          help="Causa raiz do incidente")
    parser.add_argument("--recovery",     default="",          help="Sugestao de recuperacao")
    parser.add_argument("--rollback",     action="store_true", help="Recomendar rollback")
    parser.add_argument("--note",         default="",          help="Nota de resolucao (para --resolve)")

    args = parser.parse_args()
    mgr  = AutonomousIncidentManager()

    # ── --auto-resolve ────────────────────────────────────────────────────────────
    if args.auto_resolve:
        count = mgr.auto_resolve_expired()
        if args.json:
            print(json.dumps({"auto_resolved_count": count}))
        else:
            print(f"Auto-resolved {count} incidente(s) expirado(s).")
        return

    # ── --resolve INCIDENT_ID ─────────────────────────────────────────────────────
    if args.resolve:
        ok = mgr.resolve_incident(args.resolve, args.note)
        if args.json:
            print(json.dumps({"incident_id": args.resolve, "resolved": ok}))
        else:
            if ok:
                print(f"Incidente {args.resolve} resolvido.")
            else:
                print(f"Incidente {args.resolve} nao encontrado nos ativos.")
        return

    # ── --suppress INCIDENT_ID ────────────────────────────────────────────────────
    if args.suppress:
        ok = mgr.suppress_incident(args.suppress)
        if args.json:
            print(json.dumps({"incident_id": args.suppress, "suppressed": ok}))
        else:
            if ok:
                print(f"Incidente {args.suppress} suprimido.")
            else:
                print(f"Incidente {args.suppress} nao encontrado nos ativos.")
        return

    # ── --create ──────────────────────────────────────────────────────────────────
    if args.create:
        try:
            inc = mgr.create_incident(
                subsystem               = args.subsystem,
                severity                = args.severity.upper(),
                root_cause              = args.root_cause or "Causa nao especificada",
                suggested_recovery      = args.recovery,
                rollback_recommendation = args.rollback,
            )
            if args.json:
                print(json.dumps(inc.to_dict(), indent=2))
            else:
                print(f"\nIncidente criado:")
                print(f"  incident_id:  {inc.incident_id}")
                print(f"  subsystem:    {inc.subsystem}")
                print(f"  severity:     {inc.severity} (level={inc.severity_level})")
                print(f"  root_cause:   {inc.root_cause}")
                print(f"  ttl_minutes:  {inc.ttl_minutes if inc.ttl_minutes > 0 else 'manual'}")
                print(f"  opened_at:    {inc.opened_at}")
        except ValueError as e:
            print(f"Erro: {e}")
        return

    # ── --list ────────────────────────────────────────────────────────────────────
    if args.list:
        actives = mgr.get_active_incidents()
        if args.json:
            print(json.dumps([i.to_dict() for i in actives], indent=2))
        else:
            if not actives:
                print("Nenhum incidente ativo.")
            else:
                print(f"\nIncidentes ativos ({len(actives)}):")
                for inc in actives:
                    age_str = _format_age(inc.opened_at)
                    print(
                        f"  [{inc.severity:<9}] {inc.incident_id}  "
                        f"{inc.subsystem:<20} {inc.recovery_status:<12} "
                        f"age={age_str}  {inc.root_cause[:60]}"
                    )
        return

    # ── Default: --summary ────────────────────────────────────────────────────────
    summary = mgr.get_summary()

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
        return

    print(f"\nAutonomous Incident Manager — Phase R R-6")
    print(f"  report_id:               {summary.report_id}")
    print(f"  operational_risk_score:  {summary.operational_risk_score:.1f}/100")
    print(f"  incident_severity_score: {summary.incident_severity_score:.1f}/100")
    print(f"  incident_frequency_score:{summary.incident_frequency_score:.1f}/100")
    print(f"  active_count:            {summary.active_count}")
    print(f"  open_critical:           {summary.open_critical}")
    print(f"  open_emergency:          {summary.open_emergency}")
    print(f"  resolved_last_hour:      {summary.resolved_last_hour}")
    print(f"  highest_severity:        {summary.highest_severity}")

    if summary.active_incidents:
        print(f"\n  Incidentes ativos:")
        for inc in summary.active_incidents:
            age_str = _format_age(inc.opened_at)
            print(
                f"    [{inc.severity:<9}] {inc.incident_id}  "
                f"{inc.subsystem:<20} {inc.recovery_status:<12} "
                f"age={age_str}  {inc.root_cause[:55]}"
            )
    else:
        print(f"\n  Nenhum incidente ativo.")

    print(f"\n  -> {summary.risk_assessment}")
    print(f"\n  evaluated_at: {summary.evaluated_at}")


def _format_age(opened_at: str) -> str:
    """Formata idade do incidente como string legivel."""
    try:
        opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        delta     = datetime.now(timezone.utc) - opened_dt
        total_min = int(delta.total_seconds() / 60)
        if total_min < 60:
            return f"{total_min}m"
        hours = total_min // 60
        mins  = total_min % 60
        return f"{hours}h{mins:02d}m"
    except Exception:
        return "?"


if __name__ == "__main__":
    main()

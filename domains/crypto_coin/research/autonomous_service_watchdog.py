"""
autonomous_service_watchdog.py — Phase R R-3

Service Health Watchdog.

Monitora continuamente todos os servicos do sistema, detectando anomalias
como loops travados, degradacao de latencia, deadlocks e filas acumuladas.

Scores produzidos:
  - watchdog_health_score:  saude geral dos servicos (0-100)
  - loop_integrity_score:   integridade dos loops autonomos (0-100)
  - runtime_anomaly_score:  severidade das anomalias detectadas (0=OK, 100=critico)

Servicos monitorados (via idade de arquivos e presenca de env vars):
  1.  api               — live_governance_summary.jsonl (10 min)
  2.  scheduler         — governance_history.jsonl (15 min drift check)
  3.  collectors        — live_execution_audit_log.jsonl
  4.  analytics         — behavior_audit_log.jsonl
  5.  governance_loops  — live_governance_summary.jsonl (30 min stall)
  6.  replay_engine     — live_execution_replay_log.jsonl
  7.  redis             — REDIS_URL env var
  8.  postgres          — import sqlalchemy
  9.  prometheus        — import prometheus_client
  10. exchange_api      — EXCHANGE_API_KEY ou BINANCE_API_KEY

Anomalias detectadas:
  - stalled_loop:       JSONL nao atualizado em > 30 min
  - scheduler_drift:    governance_history nao atualizado em > 15 min
  - memory_pressure:    nao verificavel sem psutil (marcado como unknown)
  - queue_buildup:      latencia media > 500ms no ultimo registro de audit

CLI:
  python -m domains.crypto_coin.research.autonomous_service_watchdog
  python -m domains.crypto_coin.research.autonomous_service_watchdog --json
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

WATCHDOG_LOG = Path("data/watchdog_log.jsonl")

# Arquivo monitorados por servico
LIVE_GOV_SUMMARY = Path("data/live_governance_summary.jsonl")
GOVERNANCE_HIST  = Path("data/governance_history.jsonl")
AUDIT_LOG        = Path("data/live_execution_audit_log.jsonl")
BEHAVIOR_LOG     = Path("data/behavior_audit_log.jsonl")
REPLAY_LOG       = Path("data/live_execution_replay_log.jsonl")

# Limites de idade (segundos)
STALL_THRESHOLD_SEC     = 30 * 60   # 30 min
API_THRESHOLD_SEC       = 10 * 60   # 10 min
SCHEDULER_DRIFT_SEC     = 15 * 60   # 15 min

# Limite de latencia (ms)
QUEUE_LATENCY_THRESHOLD = 500.0

# Prometheus (optional)
try:
    from api.runtime_metrics import (
        watchdog_health_score  as _prom_health,
        loop_integrity_score   as _prom_loop,
        runtime_anomaly_score  as _prom_anomaly,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ServiceCheck:
    """Resultado da verificacao de um servico individual."""
    name:                  str
    status:                str          # healthy | degraded | stalled | unknown | error
    detail:                str
    last_activity_seconds: float | None
    anomalies:             list[str]


@dataclass
class WatchdogReport:
    """Relatorio completo do watchdog de servicos."""
    report_id:             str
    watchdog_health_score: float    # 0-100
    loop_integrity_score:  float    # 0-100
    runtime_anomaly_score: float    # 0-100 (100 = pior)
    services:              list[ServiceCheck]
    healthy_count:         int
    degraded_count:        int
    stalled_count:         int
    unknown_count:         int
    anomalies_detected:    list[str]
    recommended_action:    str      # NONE | MONITOR | FREEZE | ESCALATE | RESTART
    evaluated_at:          str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["services"] = [asdict(s) for s in self.services]
        return d


# ── Watchdog ──────────────────────────────────────────────────────────────────

class AutonomousServiceWatchdog:
    """
    R-3: Watchdog autonomo de servicos.

    Verifica a saude de todos os servicos criticos usando proxies indiretos
    (idade de arquivos, variaveis de ambiente, imports). Detecta anomalias
    de runtime sem depender de instrumentacao interna dos servicos.
    """

    def __init__(self, watchdog_log: Path = WATCHDOG_LOG):
        self.watchdog_log = watchdog_log

    def evaluate(self) -> WatchdogReport:
        """Executa todas as verificacoes e retorna WatchdogReport."""
        report_id    = str(uuid.uuid4())[:12]
        evaluated_at = datetime.now(timezone.utc).isoformat()

        services: list[ServiceCheck] = [
            self._check_api(),
            self._check_scheduler(),
            self._check_collectors(),
            self._check_analytics(),
            self._check_governance_loops(),
            self._check_replay_engine(),
            self._check_redis(),
            self._check_postgres(),
            self._check_prometheus(),
            self._check_exchange_api(),
        ]

        healthy_count  = sum(1 for s in services if s.status == "healthy")
        degraded_count = sum(1 for s in services if s.status == "degraded")
        stalled_count  = sum(1 for s in services if s.status == "stalled")
        unknown_count  = sum(1 for s in services if s.status == "unknown")
        total_count    = len(services)

        # Coleta anomalias de todos os servicos
        all_anomalies: list[str] = []
        for svc in services:
            all_anomalies.extend(svc.anomalies)

        # Anomalia global de memory pressure
        all_anomalies.extend(self._check_memory_pressure())

        # Scores
        health_score = round((healthy_count / total_count) * 100.0, 1) if total_count else 0.0
        loop_score   = round(
            max(0.0, 100.0 - stalled_count * 20.0 - degraded_count * 10.0), 1
        )
        anomaly_score = round(min(100.0, len(all_anomalies) * 15.0), 1)

        recommended_action = self._compute_action(
            health_score, stalled_count, all_anomalies
        )

        report = WatchdogReport(
            report_id             = report_id,
            watchdog_health_score = health_score,
            loop_integrity_score  = loop_score,
            runtime_anomaly_score = anomaly_score,
            services              = services,
            healthy_count         = healthy_count,
            degraded_count        = degraded_count,
            stalled_count         = stalled_count,
            unknown_count         = unknown_count,
            anomalies_detected    = all_anomalies,
            recommended_action    = recommended_action,
            evaluated_at          = evaluated_at,
        )

        self._persist(report)

        if _METRICS_AVAILABLE:
            try:
                _prom_health.set(health_score)
                _prom_loop.set(loop_score)
                _prom_anomaly.set(anomaly_score)
            except Exception:
                pass

        return report

    # ── Service checks ────────────────────────────────────────────────────────

    def _check_api(self) -> ServiceCheck:
        """Verifica servico de API via idade do live_governance_summary.jsonl."""
        return self._check_file_age(
            name="api",
            path=LIVE_GOV_SUMMARY,
            threshold_sec=API_THRESHOLD_SEC,
            stall_sec=STALL_THRESHOLD_SEC,
            label="live_governance_summary.jsonl",
        )

    def _check_scheduler(self) -> ServiceCheck:
        """Verifica scheduler via idade do governance_history.jsonl."""
        svc = self._check_file_age(
            name="scheduler",
            path=GOVERNANCE_HIST,
            threshold_sec=SCHEDULER_DRIFT_SEC,
            stall_sec=STALL_THRESHOLD_SEC,
            label="governance_history.jsonl",
        )
        # Anomalia especifica de scheduler drift
        if svc.last_activity_seconds is not None and svc.last_activity_seconds > SCHEDULER_DRIFT_SEC:
            if "scheduler_drift" not in svc.anomalies:
                svc.anomalies.append(
                    f"scheduler_drift: governance_history nao atualizado ha "
                    f"{svc.last_activity_seconds / 60:.1f}min (>{SCHEDULER_DRIFT_SEC // 60}min)"
                )
        return svc

    def _check_collectors(self) -> ServiceCheck:
        """Verifica collectors via AUDIT_LOG e detecta queue_buildup."""
        svc = self._check_file_age(
            name="collectors",
            path=AUDIT_LOG,
            threshold_sec=API_THRESHOLD_SEC,
            stall_sec=STALL_THRESHOLD_SEC,
            label="live_execution_audit_log.jsonl",
        )
        # Detecta queue_buildup via latencia do ultimo registro
        if AUDIT_LOG.exists():
            last = self._read_last(AUDIT_LOG)
            if last:
                latency = float(last.get("avg_latency_ms", 0.0) or last.get("latency_ms", 0.0))
                if latency > QUEUE_LATENCY_THRESHOLD:
                    svc.anomalies.append(
                        f"queue_buildup: latencia_media={latency:.0f}ms "
                        f"(>{QUEUE_LATENCY_THRESHOLD:.0f}ms)"
                    )
                    if svc.status == "healthy":
                        svc.status = "degraded"
                        svc.detail += f"; latencia elevada ({latency:.0f}ms)"
        return svc

    def _check_analytics(self) -> ServiceCheck:
        """Verifica analytics via idade do behavior_audit_log.jsonl."""
        return self._check_file_age(
            name="analytics",
            path=BEHAVIOR_LOG,
            threshold_sec=API_THRESHOLD_SEC,
            stall_sec=STALL_THRESHOLD_SEC,
            label="behavior_audit_log.jsonl",
        )

    def _check_governance_loops(self) -> ServiceCheck:
        """Verifica governance_loops via live_governance_summary.jsonl (threshold 30min)."""
        return self._check_file_age(
            name="governance_loops",
            path=LIVE_GOV_SUMMARY,
            threshold_sec=STALL_THRESHOLD_SEC // 2,
            stall_sec=STALL_THRESHOLD_SEC,
            label="live_governance_summary.jsonl (loop check)",
        )

    def _check_replay_engine(self) -> ServiceCheck:
        """Verifica replay_engine via live_execution_replay_log.jsonl."""
        return self._check_file_age(
            name="replay_engine",
            path=REPLAY_LOG,
            threshold_sec=API_THRESHOLD_SEC,
            stall_sec=STALL_THRESHOLD_SEC,
            label="live_execution_replay_log.jsonl",
        )

    def _check_redis(self) -> ServiceCheck:
        """Verifica redis via presenca de REDIS_URL."""
        url = os.environ.get("REDIS_URL", "")
        if url:
            return ServiceCheck(
                name="redis",
                status="healthy",
                detail="REDIS_URL presente",
                last_activity_seconds=None,
                anomalies=[],
            )
        return ServiceCheck(
            name="redis",
            status="unknown",
            detail="REDIS_URL nao definido — conectividade nao verificavel",
            last_activity_seconds=None,
            anomalies=[],
        )

    def _check_postgres(self) -> ServiceCheck:
        """Verifica postgres via import sqlalchemy."""
        try:
            import sqlalchemy  # noqa: F401
            return ServiceCheck(
                name="postgres",
                status="healthy",
                detail="sqlalchemy disponivel",
                last_activity_seconds=None,
                anomalies=[],
            )
        except ImportError:
            return ServiceCheck(
                name="postgres",
                status="error",
                detail="sqlalchemy nao instalado — postgres indisponivel",
                last_activity_seconds=None,
                anomalies=["postgres_driver_missing: sqlalchemy nao encontrado"],
            )

    def _check_prometheus(self) -> ServiceCheck:
        """Verifica prometheus via import prometheus_client."""
        try:
            import prometheus_client  # noqa: F401
            return ServiceCheck(
                name="prometheus",
                status="healthy",
                detail="prometheus_client disponivel",
                last_activity_seconds=None,
                anomalies=[],
            )
        except ImportError:
            return ServiceCheck(
                name="prometheus",
                status="degraded",
                detail="prometheus_client nao instalado — metricas desabilitadas",
                last_activity_seconds=None,
                anomalies=[],
            )

    def _check_exchange_api(self) -> ServiceCheck:
        """Verifica exchange_api via presenca de API key."""
        key = os.environ.get("EXCHANGE_API_KEY") or os.environ.get("BINANCE_API_KEY")
        if key:
            src = "EXCHANGE_API_KEY" if os.environ.get("EXCHANGE_API_KEY") else "BINANCE_API_KEY"
            return ServiceCheck(
                name="exchange_api",
                status="healthy",
                detail=f"{src} presente",
                last_activity_seconds=None,
                anomalies=[],
            )
        return ServiceCheck(
            name="exchange_api",
            status="unknown",
            detail="EXCHANGE_API_KEY e BINANCE_API_KEY ausentes — exchange nao configurada",
            last_activity_seconds=None,
            anomalies=[],
        )

    # ── Anomaly: memory pressure ───────────────────────────────────────────────

    def _check_memory_pressure(self) -> list[str]:
        """
        Verifica pressao de memoria. Sem psutil, retorna status unknown.
        Nao adicionamos anomalia — apenas informacional.
        """
        try:
            import psutil
            mem = psutil.virtual_memory()
            if mem.percent > 90.0:
                return [f"memory_pressure: uso={mem.percent:.1f}% (>90%)"]
            return []
        except ImportError:
            # psutil nao disponivel — nao podemos verificar
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _check_file_age(
        self,
        name:          str,
        path:          Path,
        threshold_sec: float,
        stall_sec:     float,
        label:         str,
    ) -> ServiceCheck:
        """
        Verifica saude de um servico pela idade de seu arquivo JSONL.

        - healthy:  arquivo existe e foi atualizado dentro de threshold_sec
        - degraded: arquivo existe mas idade entre threshold_sec e stall_sec
        - stalled:  arquivo existe mas nao atualizado em > stall_sec
        - unknown:  arquivo nao existe
        """
        if not path.exists():
            return ServiceCheck(
                name=name,
                status="unknown",
                detail=f"{label} nao encontrado",
                last_activity_seconds=None,
                anomalies=[],
            )

        try:
            mtime     = path.stat().st_mtime
            age_sec   = time.time() - mtime
            age_min   = age_sec / 60.0
            anomalies: list[str] = []

            if age_sec > stall_sec:
                status = "stalled"
                detail = f"{label}: ultimo update ha {age_min:.1f}min (stalled >{stall_sec // 60}min)"
                anomalies.append(
                    f"stalled_loop: {name} nao atualizado ha {age_min:.1f}min"
                )
            elif age_sec > threshold_sec:
                status = "degraded"
                detail = f"{label}: ultimo update ha {age_min:.1f}min (degraded >{threshold_sec // 60}min)"
            else:
                status = "healthy"
                detail = f"{label}: atualizado ha {age_min:.1f}min"

            return ServiceCheck(
                name=name,
                status=status,
                detail=detail,
                last_activity_seconds=round(age_sec, 1),
                anomalies=anomalies,
            )
        except Exception as exc:
            return ServiceCheck(
                name=name,
                status="error",
                detail=f"Erro ao verificar {label}: {exc}",
                last_activity_seconds=None,
                anomalies=[f"check_error: {name} — {exc}"],
            )

    def _read_last(self, path: Path) -> dict | None:
        """Le ultimo registro de um JSONL."""
        last: dict | None = None
        try:
            with open(path, encoding="utf-8") as f:
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

    def _compute_action(
        self,
        health_score:  float,
        stalled_count: int,
        anomalies:     list[str],
    ) -> str:
        """Determina acao recomendada com base nos scores e contagens."""
        if stalled_count >= 5:
            return "RESTART"
        if health_score < 40.0:
            return "ESCALATE"
        if health_score < 60.0 or stalled_count >= 3:
            return "FREEZE"
        if health_score < 80.0:
            return "MONITOR"
        return "NONE"

    def _persist(self, report: WatchdogReport) -> None:
        try:
            self.watchdog_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "evaluated_at":          report.evaluated_at,
                "report_id":             report.report_id,
                "watchdog_health_score": report.watchdog_health_score,
                "loop_integrity_score":  report.loop_integrity_score,
                "runtime_anomaly_score": report.runtime_anomaly_score,
                "healthy_count":         report.healthy_count,
                "degraded_count":        report.degraded_count,
                "stalled_count":         report.stalled_count,
                "unknown_count":         report.unknown_count,
                "anomalies_count":       len(report.anomalies_detected),
                "recommended_action":    report.recommended_action,
            }
            with open(self.watchdog_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Service Watchdog — Phase R R-3"
    )
    parser.add_argument("--json", action="store_true", help="Saida em JSON")
    args = parser.parse_args()

    watchdog = AutonomousServiceWatchdog()
    report   = watchdog.evaluate()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    action_icon = {
        "NONE":      "[OK]",
        "MONITOR":   "[~~]",
        "FREEZE":    "[!!]",
        "ESCALATE":  "[XX]",
        "RESTART":   "[RR]",
    }.get(report.recommended_action, "[??]")

    status_icons = {
        "healthy":  "[OK]",
        "degraded": "[~~]",
        "stalled":  "[XX]",
        "unknown":  "[??]",
        "error":    "[EE]",
    }

    print(f"\nAutonomous Service Watchdog — Phase R R-3")
    print(f"  report_id:             {report.report_id}")
    print(f"  watchdog_health_score: {report.watchdog_health_score:.1f}/100")
    print(f"  loop_integrity_score:  {report.loop_integrity_score:.1f}/100")
    print(f"  runtime_anomaly_score: {report.runtime_anomaly_score:.1f}/100")
    print(f"  recommended_action:    {action_icon} {report.recommended_action}")
    print(f"\n  Servicos ({len(report.services)}):")
    print(f"    healthy={report.healthy_count}  degraded={report.degraded_count}  "
          f"stalled={report.stalled_count}  unknown={report.unknown_count}")
    print()
    for svc in report.services:
        icon = status_icons.get(svc.status, "[??]")
        age  = f" ({svc.last_activity_seconds:.0f}s)" if svc.last_activity_seconds is not None else ""
        print(f"    {icon} {svc.name:<20} {svc.status:<10}{age}")
        if svc.anomalies:
            for a in svc.anomalies:
                print(f"         [!] {a}")

    if report.anomalies_detected:
        print(f"\n  Anomalias detectadas ({len(report.anomalies_detected)}):")
        for a in report.anomalies_detected:
            print(f"    [!] {a}")
    else:
        print(f"\n  Sem anomalias detectadas.")

    print(f"\n  Avaliado em: {report.evaluated_at}")


if __name__ == "__main__":
    main()

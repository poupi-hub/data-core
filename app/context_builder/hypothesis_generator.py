"""
HypothesisGenerator — gera hipóteses de causa raiz a partir do contexto coletado.

Regras determinísticas baseadas em evidências:
  - OOM confirmado no dmesg → oom_kill com alta confiança
  - Redis down → redis_unavailable
  - Postgres com lock waits → database_issue
  - Deploy recente antes do incidente → deployment_failure possível
  - Scheduler frozen + heartbeat stale → scheduler_frozen
  - Log com traceback → crash_loop

Cada hipótese tem:
  - hypothesis: descrição legível
  - bucket: root_cause_bucket canônico (compatível com incident_history)
  - confidence: 0.0 – 1.0
  - evidence: lista de evidências suportando a hipótese
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Hypothesis:
    hypothesis: str
    bucket: str
    confidence: float
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "bucket": self.bucket,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
        }


class HypothesisGenerator:
    """
    Gera hipóteses ordenadas por confiança decrescente.
    Não muta o contexto — apenas lê e analisa.
    """

    def generate(self, context_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """
        context_snapshot: saída do ContextBuilderService.build()
        Retorna lista de hipóteses ordenada por confidence desc.
        """
        sources = context_snapshot.get("sources", {})
        hypotheses: list[Hypothesis] = []

        hypotheses.extend(self._check_oom(sources))
        hypotheses.extend(self._check_redis(sources))
        hypotheses.extend(self._check_postgres(sources))
        hypotheses.extend(self._check_scheduler(sources))
        hypotheses.extend(self._check_deploy(sources))
        hypotheses.extend(self._check_health(sources))
        hypotheses.extend(self._check_logs(sources))

        # Dedup e ordenar por confiança
        seen_buckets: set[str] = set()
        unique: list[Hypothesis] = []
        for h in sorted(hypotheses, key=lambda x: x.confidence, reverse=True):
            if h.bucket not in seen_buckets:
                seen_buckets.add(h.bucket)
                unique.append(h)

        return [h.to_dict() for h in unique]

    # ── Individual checks ─────────────────────────────────────────────────────

    def _check_oom(self, sources: dict) -> list[Hypothesis]:
        logs = sources.get("logs", {}).get("data", {})
        has_oom = logs.get("has_oom", False)
        if not has_oom:
            return []
        return [Hypothesis(
            hypothesis="Processo morreu por OOM kill do kernel",
            bucket="oom_kill",
            confidence=0.92,
            evidence=logs.get("dmesg_errors", [])[:3],
        )]

    def _check_redis(self, sources: dict) -> list[Hypothesis]:
        redis = sources.get("redis", {}).get("data", {})
        all_connected = redis.get("all_connected", True)
        if all_connected:
            return []
        disconnected = [
            name for name, info in redis.get("instances", {}).items()
            if not info.get("connected", True)
        ]
        return [Hypothesis(
            hypothesis=f"Redis inacessível: {', '.join(disconnected)}",
            bucket="redis_unavailable",
            confidence=0.95,
            evidence=[f"Redis {n} not connected" for n in disconnected],
        )]

    def _check_postgres(self, sources: dict) -> list[Hypothesis]:
        pg = sources.get("postgres", {}).get("data", {})
        hypotheses = []

        if not pg.get("connected", True):
            hypotheses.append(Hypothesis(
                hypothesis="PostgreSQL inacessível",
                bucket="database_issue",
                confidence=0.93,
                evidence=pg.get("errors", []),
            ))
        elif pg.get("has_lock_waits", False):
            locks = pg.get("data", {}).get("connection_count", {}).get("waiting_lock", 0)
            hypotheses.append(Hypothesis(
                hypothesis=f"PostgreSQL com {locks} conexões aguardando lock",
                bucket="database_issue",
                confidence=0.70,
                evidence=[f"{locks} connections waiting for lock"],
            ))

        if pg.get("has_long_queries", False):
            long_qs = pg.get("data", {}).get("long_queries", [])
            if long_qs:
                worst = long_qs[0]
                hypotheses.append(Hypothesis(
                    hypothesis=f"Query lenta bloqueando DB: {worst.get('duration_seconds', 0)}s",
                    bucket="database_issue",
                    confidence=0.55,
                    evidence=[f"Query running for {worst.get('duration_seconds')}s: {worst.get('query', '')[:80]}"],
                ))

        return hypotheses

    def _check_scheduler(self, sources: dict) -> list[Hypothesis]:
        sched = sources.get("scheduler", {}).get("data", {})
        relevant = sources.get("scheduler", {}).get("relevant", False)
        if not relevant:
            return []

        hypotheses = []

        if sched.get("is_oom", False):
            hypotheses.append(Hypothesis(
                hypothesis="Scheduler data-core teve OOM recente (state=4)",
                bucket="oom_kill",
                confidence=0.88,
                evidence=["data_core_scheduler_state = 4 (OOM_RECENT)"],
            ))
        elif sched.get("is_frozen", False):
            hypotheses.append(Hypothesis(
                hypothesis="APScheduler congelado ou em estado degradado",
                bucket="scheduler_frozen",
                confidence=0.82,
                evidence=[
                    f"scheduler_state = {sched.get('data', {}).get('metrics', {}).get('state')}",
                    f"heartbeat_age = {sched.get('data', {}).get('metrics', {}).get('heartbeat_age_seconds')}s",
                ],
            ))
        elif sched.get("heartbeat_stale", False):
            heartbeat_age = (
                sched.get("data", {}).get("metrics", {}).get("heartbeat_age_seconds", 0)
            )
            hypotheses.append(Hypothesis(
                hypothesis=f"Scheduler heartbeat stale: {heartbeat_age:.0f}s",
                bucket="scheduler_frozen",
                confidence=0.65,
                evidence=[f"scheduler_heartbeat_age_seconds = {heartbeat_age}"],
            ))

        return hypotheses

    def _check_deploy(self, sources: dict) -> list[Hypothesis]:
        deploy = sources.get("deploy", {}).get("data", {})
        if not deploy.get("deploy_possibly_caused_incident", False):
            return []
        recent = deploy.get("recent_deploy_before_incident")
        if not recent:
            return []
        return [Hypothesis(
            hypothesis=f"Deploy recente antes do incidente: {recent.get('subject', '')}",
            bucket="deployment_failure",
            confidence=0.55,
            evidence=[
                f"Commit {recent.get('hash')} by {recent.get('author')} at {recent.get('date')}",
                recent.get("subject", ""),
            ],
        )]

    def _check_health(self, sources: dict) -> list[Hypothesis]:
        health = sources.get("health", {}).get("data", {})
        if health.get("service_reachable", True):
            return []
        return [Hypothesis(
            hypothesis="Serviço inacessível — /health não respondeu",
            bucket="crash_loop",
            confidence=0.80,
            evidence=health.get("errors", [])[:3],
        )]

    def _check_logs(self, sources: dict) -> list[Hypothesis]:
        logs = sources.get("logs", {}).get("data", {})
        error_lines = logs.get("error_lines", [])
        if not error_lines:
            return []

        hypotheses = []

        # Detectar padrões específicos nas linhas de erro
        combined = " ".join(error_lines).lower()

        if "traceback" in combined or "exception" in combined:
            hypotheses.append(Hypothesis(
                hypothesis="Exception não tratada nos logs — possível crash loop",
                bucket="crash_loop",
                confidence=0.60,
                evidence=error_lines[:3],
            ))

        if "connection refused" in combined or "econnrefused" in combined:
            hypotheses.append(Hypothesis(
                hypothesis="Conexão recusada nos logs — dependência inacessível",
                bucket="network_issue",
                confidence=0.65,
                evidence=[l for l in error_lines if "refused" in l.lower()][:2],
            ))

        if "out of memory" in combined or "cannot allocate" in combined:
            hypotheses.append(Hypothesis(
                hypothesis="OOM detectado nos logs da aplicação",
                bucket="oom_kill",
                confidence=0.75,
                evidence=[l for l in error_lines if "memory" in l.lower()][:2],
            ))

        return hypotheses

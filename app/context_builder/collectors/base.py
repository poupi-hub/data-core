"""
Base class para todos os collectors do Context Builder.

Cada collector:
  - É responsável por um domínio (logs, metrics, health, redis, postgres, deploy, scheduler)
  - Possui um timeout próprio
  - Retorna um CollectorResult padronizado
  - Nunca lança exceção — erros são capturados e incluídos no resultado
  - Executa apenas operações READ-ONLY
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CollectorResult:
    """Resultado padronizado de um collector."""
    source: str                          # nome do collector (ex: "logs", "health")
    success: bool                        # True se coletou dados úteis
    data: dict[str, Any] = field(default_factory=dict)   # dados coletados
    error: str | None = None             # mensagem de erro se failed
    duration_ms: float = 0.0            # tempo de execução em ms
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
            "warnings": self.warnings,
        }


class BaseCollector:
    """
    Collector base — implementar collect_data() nas subclasses.
    """
    name: str = "base"
    timeout_seconds: float = 5.0

    def collect(self, context: dict[str, Any]) -> CollectorResult:
        """
        Executa a coleta com tratamento de erro e timing.
        context: metadados do alerta (alert_id, service, labels, etc.)
        """
        t0 = time.perf_counter()
        try:
            data = self.collect_data(context)
            duration_ms = (time.perf_counter() - t0) * 1000
            return CollectorResult(
                source=self.name,
                success=True,
                data=data,
                duration_ms=duration_ms,
            )
        except TimeoutError as exc:
            duration_ms = (time.perf_counter() - t0) * 1000
            return CollectorResult(
                source=self.name,
                success=False,
                error=f"Timeout after {self.timeout_seconds}s: {exc}",
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - t0) * 1000
            return CollectorResult(
                source=self.name,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration_ms,
            )

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        """Implementar nas subclasses. Retornar dict com dados coletados."""
        raise NotImplementedError

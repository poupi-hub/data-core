"""
strategy_registry.py — Phase H Fase 7

Interface Python para o strategy_registry.yaml.

Permite ao TradingBot e ao research layer:
  - Carregar parâmetros canônicos de uma estratégia por ID
  - Consultar status e histórico de performance
  - Listar estratégias ativas / em pesquisa
  - Registrar novo resultado de performance

Reutiliza: strategy_registry.yaml (fonte única de verdade)
Complementa: experiment_tracker.py (rastreamento de execuções individuais)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Evita dependência em YAML se não disponível — usa json como fallback
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

REGISTRY_PATH = Path(__file__).parent / "strategy_registry.yaml"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class StrategyPerformance:
    symbol:            str
    timeframe:         str
    dataset:           str
    days:              int
    sharpe:            float
    sortino:           float
    calmar:            float
    max_drawdown:      float
    total_trades:      int
    win_rate:          float
    total_return_pct:  float
    recorded_at:       str


@dataclass
class StrategyDefinition:
    id:                  str
    name:                str
    description:         str
    version:             str
    status:              str   # active | research | archived | candidate
    parameters:          dict[str, Any]
    performance_history: list[StrategyPerformance]
    notes:               str
    created_at:          str
    last_updated:        str

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyDefinition":
        perf_history = [
            StrategyPerformance(**p)
            for p in data.get("performance_history", [])
        ]
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            version=data.get("version", "v1.0"),
            status=data.get("status", "research"),
            parameters=data.get("parameters", {}),
            performance_history=perf_history,
            notes=data.get("notes", ""),
            created_at=data.get("created_at", ""),
            last_updated=data.get("last_updated", ""),
        )

    def best_performance(self, metric: str = "sharpe") -> StrategyPerformance | None:
        if not self.performance_history:
            return None
        return max(self.performance_history, key=lambda p: getattr(p, metric, 0.0))

    def is_active(self) -> bool:
        return self.status == "active"

    def is_research(self) -> bool:
        return self.status in ("research", "candidate")


# ── Registry ─────────────────────────────────────────────────────────────────

class StrategyRegistry:
    """
    Interface para o catálogo de estratégias.

    Carrega do YAML na primeira chamada (lazy load).
    Imutável durante a sessão — alterações requerem restart ou reload().
    """

    def __init__(self, registry_path: Path = REGISTRY_PATH) -> None:
        self._path = registry_path
        self._strategies: dict[str, StrategyDefinition] | None = None

    def _load(self) -> dict[str, StrategyDefinition]:
        if not self._path.exists():
            raise FileNotFoundError(f"Strategy registry não encontrado: {self._path}")

        if _HAS_YAML:
            with open(self._path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        else:
            raise ImportError(
                "PyYAML não instalado. Execute: pip install pyyaml\n"
                f"Arquivo: {self._path}"
            )

        result = {}
        for s in raw.get("strategies", []):
            strategy = StrategyDefinition.from_dict(s)
            result[strategy.id] = strategy

        return result

    def _ensure_loaded(self) -> dict[str, StrategyDefinition]:
        if self._strategies is None:
            self._strategies = self._load()
        return self._strategies

    def reload(self) -> None:
        """Recarrega o registry do disco."""
        self._strategies = self._load()

    # ── Queries ───────────────────────────────────────────────────────────

    def get(self, strategy_id: str) -> StrategyDefinition:
        """
        Retorna a definição de uma estratégia pelo ID.
        Lança KeyError se não encontrada.
        """
        strategies = self._ensure_loaded()
        if strategy_id not in strategies:
            available = list(strategies.keys())
            raise KeyError(
                f"Estratégia '{strategy_id}' não encontrada. "
                f"Disponíveis: {available}"
            )
        return strategies[strategy_id]

    def get_parameters(self, strategy_id: str) -> dict[str, Any]:
        """Retorna os parâmetros canônicos de uma estratégia."""
        return self.get(strategy_id).parameters.copy()

    def list_all(self) -> list[StrategyDefinition]:
        """Lista todas as estratégias registradas."""
        return list(self._ensure_loaded().values())

    def list_active(self) -> list[StrategyDefinition]:
        """Lista estratégias com status 'active'."""
        return [s for s in self._ensure_loaded().values() if s.is_active()]

    def list_research(self) -> list[StrategyDefinition]:
        """Lista estratégias em pesquisa."""
        return [s for s in self._ensure_loaded().values() if s.is_research()]

    def summary(self) -> dict[str, Any]:
        """Sumário do registry: total, ativos, em pesquisa."""
        strategies = self._ensure_loaded()
        return {
            "total":    len(strategies),
            "active":   sum(1 for s in strategies.values() if s.is_active()),
            "research": sum(1 for s in strategies.values() if s.is_research()),
            "archived": sum(1 for s in strategies.values() if s.status == "archived"),
            "ids":      list(strategies.keys()),
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

_registry: StrategyRegistry | None = None


def get_registry(registry_path: Optional[Path] = None) -> StrategyRegistry:
    """Retorna instância singleton do registry."""
    global _registry
    if _registry is None:
        _registry = StrategyRegistry(registry_path or REGISTRY_PATH)
    return _registry


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Strategy Registry — CLI")
    parser.add_argument("--list",     action="store_true", help="Listar todas as estratégias")
    parser.add_argument("--active",   action="store_true", help="Listar estratégias ativas")
    parser.add_argument("--get",      type=str,            help="Detalhes de uma estratégia por ID")
    parser.add_argument("--params",   type=str,            help="Parâmetros de uma estratégia por ID")
    parser.add_argument("--summary",  action="store_true", help="Sumário do registry")
    parser.add_argument("--json",     action="store_true", help="Output em JSON")
    args = parser.parse_args()

    registry = get_registry()

    if args.summary:
        summary = registry.summary()
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"Total: {summary['total']} | Ativas: {summary['active']} | Pesquisa: {summary['research']}")
            print(f"IDs: {summary['ids']}")
        return

    if args.list or args.active:
        strategies = registry.list_active() if args.active else registry.list_all()
        if args.json:
            print(json.dumps([
                {"id": s.id, "name": s.name, "version": s.version, "status": s.status}
                for s in strategies
            ], indent=2))
        else:
            for s in strategies:
                best = s.best_performance()
                perf_str = f"sharpe={best.sharpe:.2f}" if best else "sem dados"
                print(f"  [{s.status.upper():8s}] {s.id:<25} v{s.version:<8} {perf_str}")
        return

    if args.params:
        try:
            params = registry.get_parameters(args.params)
            print(json.dumps(params, indent=2))
        except KeyError as e:
            print(f"Erro: {e}")
        return

    if args.get:
        try:
            s = registry.get(args.get)
            if args.json:
                print(json.dumps({
                    "id": s.id, "name": s.name, "version": s.version,
                    "status": s.status, "parameters": s.parameters,
                    "notes": s.notes,
                    "performance_history": [
                        {k: getattr(p, k) for k in p.__dataclass_fields__}
                        for p in s.performance_history
                    ],
                }, indent=2, default=str))
            else:
                print(f"{s.name} ({s.id}) — {s.status}")
                print(f"Versão: {s.version} | Atualizado: {s.last_updated}")
                print(f"Parâmetros: {json.dumps(s.parameters, indent=2)}")
                if s.performance_history:
                    print("\nPerformance history:")
                    for p in s.performance_history:
                        print(f"  {p.symbol} {p.timeframe} | sharpe={p.sharpe:.2f} sortino={p.sortino:.2f} calmar={p.calmar:.2f} dd={p.max_drawdown:.3f}")
                print(f"\nNotas: {s.notes[:200]}")
        except KeyError as e:
            print(f"Erro: {e}")
        return

    parser.print_help()


if __name__ == "__main__":
    _main()

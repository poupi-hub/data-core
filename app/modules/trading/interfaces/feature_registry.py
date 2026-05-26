"""Feature registry contract for ML feature management.

Provides an abstract interface for registering, computing, and listing named
feature functions.  The ``InMemoryFeatureRegistry`` is a lightweight reference
implementation that requires no external dependencies and can be used in unit
tests and exploratory analysis.

Usage
─────
    registry = InMemoryFeatureRegistry()

    def rsi_feature(candles):
        # compute RSI from candles DataFrame
        ...

    registry.register("rsi_14", rsi_feature, "14-period RSI")
    result = registry.compute("rsi_14", candles_df)
    print(registry.list_features())

Design notes
────────────
- The ``compute`` function accepts *any* data type for ``data`` so that
  callers are not forced to use pandas.  Concrete implementations may
  narrow the type annotation.
- No pandas / numpy import here — keep stdlib-only at the contract layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable


class FeatureRegistry(ABC):
    """Abstract registry for named feature compute functions."""

    @abstractmethod
    def register(
        self,
        name: str,
        compute_fn: Callable[..., Any],
        description: str = "",
    ) -> None:
        """Register a feature computation function under ``name``.

        Args:
            name: Unique feature identifier (e.g. "rsi_14", "atr_norm").
            compute_fn: Callable that takes raw data and returns a feature value.
            description: Human-readable description of the feature.

        Raises:
            ValueError: If ``name`` is already registered.
        """

    @abstractmethod
    def compute(self, name: str, data: Any) -> Any:
        """Invoke the registered function for ``name`` with ``data``.

        Args:
            name: Feature identifier previously passed to ``register``.
            data: Input data forwarded to the registered function.

        Returns:
            Whatever the registered function returns.

        Raises:
            KeyError: If ``name`` is not registered.
        """

    @abstractmethod
    def list_features(self) -> list[str]:
        """Return a sorted list of registered feature names."""

    @abstractmethod
    def describe(self, name: str) -> str:
        """Return the description for a registered feature.

        Raises:
            KeyError: If ``name`` is not registered.
        """


class InMemoryFeatureRegistry(FeatureRegistry):
    """Lightweight in-memory implementation of FeatureRegistry.

    Intended for unit tests and local exploratory work.  Not thread-safe.
    """

    def __init__(self) -> None:
        self._fns: dict[str, Callable[..., Any]] = {}
        self._descs: dict[str, str] = {}

    def register(
        self,
        name: str,
        compute_fn: Callable[..., Any],
        description: str = "",
    ) -> None:
        if name in self._fns:
            raise ValueError(f"Feature '{name}' is already registered.")
        self._fns[name] = compute_fn
        self._descs[name] = description

    def compute(self, name: str, data: Any) -> Any:
        if name not in self._fns:
            raise KeyError(f"Feature '{name}' is not registered.")
        return self._fns[name](data)

    def list_features(self) -> list[str]:
        return sorted(self._fns.keys())

    def describe(self, name: str) -> str:
        if name not in self._descs:
            raise KeyError(f"Feature '{name}' is not registered.")
        return self._descs[name]

    def __len__(self) -> int:
        return len(self._fns)

    def __contains__(self, name: object) -> bool:
        return name in self._fns

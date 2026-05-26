"""Feature engineering contract for ML signal models.

Defines the ``FeatureEngineer`` abstract base class that transforms raw OHLCV
candle data into ML-ready feature vectors.

Conventions
───────────
- Input is typed as ``Any`` at the contract layer to avoid a hard pandas
  dependency.  Concrete subclasses should narrow the type to
  ``pd.DataFrame`` and document their expected column schema.
- ``fit`` must be called before ``transform`` — implementations should raise
  ``RuntimeError`` if ``transform`` is called on an unfitted engineer.
- ``feature_names`` returns the ordered list of output column names.
  This list must be stable across calls to the same fitted instance.
- ``fit_transform`` is provided as a convenience method that chains
  ``fit`` and ``transform``.  Subclasses may override for efficiency.

Expected candle DataFrame schema (when using pandas)
────────────────────────────────────────────────────
Columns: timestamp (datetime, UTC index), open, high, low, close, volume
The concrete implementation is responsible for validating its own inputs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class FeatureEngineer(ABC):
    """Abstract base for OHLCV → feature-vector transformations."""

    @abstractmethod
    def fit(self, candles: Any) -> "FeatureEngineer":
        """Compute scaling parameters or any fit-time statistics from ``candles``.

        Args:
            candles: Historical OHLCV data used for fitting (e.g. pd.DataFrame).

        Returns:
            Self, to allow chaining: ``engineer.fit(df).transform(df)``.
        """

    @abstractmethod
    def transform(self, candles: Any) -> Any:
        """Apply the fitted transformation to produce feature vectors.

        Args:
            candles: OHLCV data to transform (same schema as ``fit`` input).

        Returns:
            Feature matrix (e.g. pd.DataFrame with ``feature_names()`` columns).

        Raises:
            RuntimeError: If called before ``fit``.
        """

    @abstractmethod
    def feature_names(self) -> list[str]:
        """Return the ordered list of output feature column names.

        This list must remain stable across calls to the same fitted instance.
        """

    def fit_transform(self, candles: Any) -> Any:
        """Convenience: fit on ``candles`` then return transformed result.

        Equivalent to ``self.fit(candles).transform(candles)``.
        Subclasses may override to compute fit and transform in a single pass.
        """
        return self.fit(candles).transform(candles)

    @property
    def is_fitted(self) -> bool:
        """True if ``fit`` has been called at least once.

        Default implementation checks for the presence of a ``_fitted``
        attribute.  Subclasses should set ``self._fitted = True`` in ``fit``.
        """
        return getattr(self, "_fitted", False)

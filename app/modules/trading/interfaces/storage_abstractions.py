"""Storage abstractions for ML model artifacts.

Defines the ``ModelStore`` contract for persisting and loading serialised model
weights, hyperparameter configs, or any binary artifact alongside structured
metadata.

Implementations
───────────────
- A filesystem-based store (``LocalModelStore``) is provided as a reference.
  Production systems would swap this for an S3-backed or database-backed store.
- No external dependencies beyond stdlib at this layer.

Versioning convention
─────────────────────
Each artifact is identified by a ``(model_id, version)`` tuple.
``model_id`` names the model family (e.g. "signal_classifier_sol_1h").
``version`` is a free-form string — typically a datetime stamp or semantic
version tag.  Use ``list_versions`` to enumerate all saved versions.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ModelStore(ABC):
    """Abstract contract for ML model artifact persistence."""

    @abstractmethod
    def save(
        self,
        model_id: str,
        version: str,
        artifact: bytes,
        metadata: dict[str, Any],
    ) -> None:
        """Persist a serialised model artifact with metadata.

        Args:
            model_id: Model family identifier (e.g. "signal_classifier_sol").
            version: Version tag (e.g. "2026-05-26T12:00:00", "v1.2.3").
            artifact: Raw bytes of the serialised model (pickle, ONNX, etc.).
            metadata: Arbitrary JSON-serialisable metadata dict.

        Raises:
            IOError: If the artifact cannot be persisted.
        """

    @abstractmethod
    def load(self, model_id: str, version: str) -> tuple[bytes, dict[str, Any]]:
        """Load a previously saved artifact and its metadata.

        Args:
            model_id: Model family identifier.
            version: Exact version tag to load.

        Returns:
            Tuple of (artifact_bytes, metadata_dict).

        Raises:
            KeyError: If the model_id / version combination does not exist.
        """

    @abstractmethod
    def list_versions(self, model_id: str) -> list[str]:
        """Return all stored version tags for ``model_id``, sorted ascending.

        Returns an empty list (not an error) if ``model_id`` has no versions.
        """

    @abstractmethod
    def delete(self, model_id: str, version: str) -> None:
        """Remove a specific version from the store.

        Raises:
            KeyError: If the model_id / version combination does not exist.
        """


class LocalModelStore(ModelStore):
    """Filesystem-based ModelStore for development and testing.

    Stores each artifact as two files under ``<root>/<model_id>/<version>/``:
      - ``artifact.bin``  — raw bytes
      - ``metadata.json`` — JSON metadata

    Not suitable for concurrent writers without external locking.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _dir(self, model_id: str, version: str) -> Path:
        return self._root / model_id / version

    def save(
        self,
        model_id: str,
        version: str,
        artifact: bytes,
        metadata: dict[str, Any],
    ) -> None:
        path = self._dir(model_id, version)
        path.mkdir(parents=True, exist_ok=True)
        (path / "artifact.bin").write_bytes(artifact)
        (path / "metadata.json").write_text(
            json.dumps(metadata, indent=2, default=str),
            encoding="utf-8",
        )

    def load(self, model_id: str, version: str) -> tuple[bytes, dict[str, Any]]:
        path = self._dir(model_id, version)
        if not path.exists():
            raise KeyError(f"No artifact found for {model_id!r} version {version!r}.")
        artifact = (path / "artifact.bin").read_bytes()
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        return artifact, metadata

    def list_versions(self, model_id: str) -> list[str]:
        model_dir = self._root / model_id
        if not model_dir.exists():
            return []
        versions = sorted(
            entry.name
            for entry in model_dir.iterdir()
            if entry.is_dir() and (entry / "artifact.bin").exists()
        )
        return versions

    def delete(self, model_id: str, version: str) -> None:
        path = self._dir(model_id, version)
        if not path.exists():
            raise KeyError(f"No artifact found for {model_id!r} version {version!r}.")
        for fname in ("artifact.bin", "metadata.json"):
            fpath = path / fname
            if fpath.exists():
                os.remove(fpath)
        try:
            path.rmdir()           # remove version dir if now empty
        except OSError:
            pass

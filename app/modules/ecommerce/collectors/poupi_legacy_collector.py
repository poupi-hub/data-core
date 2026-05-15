import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.raw.service import RawCollectionService
from database.models import RunStatus

logger = logging.getLogger(__name__)


POUPI_LEGACY_SCRAPERS = [
    "amazon",
    # "mercadolivre",  # scraper instável — excluído temporariamente
    "kabum",
    "magalu",
    "drogasil",
    "drogaraia",
    "paguemenos",
    "nissei",
    "ultrafarma",
    "drogariaspacheco",
    "drogariasaopaulo",
    "consultaremedios",
    "farma22",
    "panvel",
]


@dataclass(frozen=True)
class LegacyPoupiTarget:
    url: str
    source_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PoupiLegacyRawCollector:
    """Thin adapter around Poupi's existing TypeScript scrapers.

    This class intentionally does not inherit a collector base class. Its only
    Data Core contract is saving the raw scraper output through RawCollectionService.
    """

    module = "ecommerce"
    collector_name = "poupi_legacy_raw_collector"
    collector_version = "1.0.0"
    raw_schema_name = "scrapedProduct"
    raw_schema_version = "1.0.0"

    def __init__(
        self,
        db: Session,
        *,
        backend_dir: Path | None = None,
        timeout_seconds: int = 45,
        retry_attempts: int = 2,
        retry_backoff_seconds: float = 2.0,
        delay_seconds: float = 0.0,
    ) -> None:
        self.db = db
        self.raw = RawCollectionService(db)
        self.backend_dir = backend_dir or self._default_backend_dir()
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.delay_seconds = max(0.0, delay_seconds)

    def collect_targets(self, targets: list[LegacyPoupiTarget]) -> dict[str, int]:
        run = self.raw.start_run(
            module=self.module,
            source_name="poupi_legacy",
            collector_name=self.collector_name,
            collector_version=self.collector_version,
            raw_schema_name=self.raw_schema_name,
            raw_schema_version=self.raw_schema_version,
            metadata={"target_count": len(targets)},
        )
        raw_saved = 0
        errors = 0
        error_message: str | None = None
        retry_error_count = 0

        for index, target in enumerate(targets):
            source_name = target.source_name or self._guess_source_name(target.url)
            try:
                payload, attempts = self._run_legacy_scraper_with_retries(target.url, source_name)
                retry_error_count += max(attempts - 1, 0)
                raw = self.raw.save_json(
                    module=self.module,
                    source_name=source_name,
                    collector_name=self.collector_name,
                    collector_version=self.collector_version,
                    raw_schema_name=self.raw_schema_name,
                    raw_schema_version=self.raw_schema_version,
                    source_type="legacy_ts_scraper",
                    target_url=target.url,
                    endpoint=target.url,
                    raw_json=payload,
                    metadata={
                        "legacy_backend_dir": str(self.backend_dir),
                        "attempt_count": attempts,
                        "max_attempts": self.retry_attempts,
                        **target.metadata,
                    },
                )
                raw_saved += 1 if getattr(raw, "_raw_was_created", True) else 0
            except Exception as exc:
                errors += 1
                error_message = str(exc)
                self.raw.save_error(
                    module=self.module,
                    source_name=source_name,
                    collector_name=self.collector_name,
                    collector_version=self.collector_version,
                    raw_schema_name="collectionError",
                    raw_schema_version="1.0.0",
                    source_type="legacy_ts_scraper",
                    target_url=target.url,
                    endpoint=target.url,
                    error_message=str(exc),
                    metadata=target.metadata,
                )
                logger.exception("Poupi legacy scraper failed", extra={"collector": self.collector_name, "url": target.url})
            finally:
                if self.delay_seconds and index < len(targets) - 1:
                    time.sleep(self.delay_seconds)

        status = RunStatus.success if errors == 0 else RunStatus.partial if raw_saved else RunStatus.failed
        self.raw.finish_run(
            run,
            status=status,
            raw_saved_count=raw_saved,
            error_count=errors,
            error_message=error_message,
        )
        run.metadata_json = {
            **(run.metadata_json or {}),
            "duplicate_raw_count": max(len(targets) - raw_saved - errors, 0),
            "retry_error_count": retry_error_count,
            "max_attempts": self.retry_attempts,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "delay_seconds": self.delay_seconds,
            "collector_version": self.collector_version,
            "raw_schema_name": self.raw_schema_name,
            "raw_schema_version": self.raw_schema_version,
        }
        self.db.commit()
        return {"raw_saved_count": raw_saved, "error_count": errors}

    def _run_legacy_scraper_with_retries(self, url: str, source_name: str) -> tuple[dict[str, Any], int]:
        last_error: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return self._run_legacy_scraper(url, source_name), attempt
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_attempts:
                    break
                sleep_seconds = self.retry_backoff_seconds * attempt
                logger.warning(
                    "Poupi legacy scraper attempt failed; retrying",
                    extra={
                        "collector": self.collector_name,
                        "source_name": source_name,
                        "url": url,
                        "attempt": attempt,
                        "max_attempts": self.retry_attempts,
                        "sleep_seconds": sleep_seconds,
                    },
                )
                if sleep_seconds:
                    time.sleep(sleep_seconds)
        assert last_error is not None
        raise last_error

    def _run_legacy_scraper(self, url: str, source_name: str) -> dict[str, Any]:
        command = self._legacy_scraper_command(url, source_name)
        completed = subprocess.run(
            command,
            cwd=self.backend_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0 and not completed.stdout.strip():
            raise RuntimeError(completed.stderr.strip() or f"legacy scraper exited with {completed.returncode}")
        try:
            payload = json.loads(completed.stdout.strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"legacy scraper returned invalid JSON: {completed.stdout[:500]}") from exc
        if completed.returncode != 0:
            raise RuntimeError(payload.get("error") or completed.stderr.strip() or "legacy scraper failed")
        return payload

    def _legacy_scraper_command(self, url: str, source_name: str) -> list[str]:
        compiled_bridge = self.backend_dir / "dist" / "src" / "crawler" / "scrapers" / "raw-bridge.js"
        if compiled_bridge.exists():
            return [
                shutil.which("node.cmd") or shutil.which("node") or "node",
                str(compiled_bridge.relative_to(self.backend_dir)),
                url,
                source_name,
            ]
        return [
            shutil.which("npx.cmd") or shutil.which("npx") or "npx",
            "ts-node",
            "-r",
            "tsconfig-paths/register",
            "src/crawler/scrapers/raw-bridge.ts",
            url,
            source_name,
        ]

    @staticmethod
    def _guess_source_name(url: str) -> str:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or "unknown"
        return host.replace("www.", "").split(".")[0]

    @staticmethod
    def _default_backend_dir() -> Path:
        configured = os.getenv("POUPI_LEGACY_BACKEND_DIR")
        if configured:
            return Path(configured)
        # Try sibling repo layout: data-core/ and poupi-baby/ side by side
        sibling = Path(__file__).resolve().parents[4] / "poupi-baby" / "backend"
        if sibling.exists():
            return sibling
        raise RuntimeError(
            "Poupi-baby backend directory not found. "
            "Set POUPI_LEGACY_BACKEND_DIR env var to the absolute path of poupi-baby/backend."
        )

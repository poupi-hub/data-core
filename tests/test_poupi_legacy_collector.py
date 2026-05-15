"""Unit tests for PoupiLegacyRawCollector.

Mocks subprocess.run so no Node.js runtime or live URLs are needed.
Uses a real DB session for RawCollectionService persistence.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.modules.ecommerce.collectors.poupi_legacy_collector import (
    LegacyPoupiTarget,
    PoupiLegacyRawCollector,
)
from app.raw.models import RawCollection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_payload(store: str = "drogasil", price: float = 49.9) -> str:
    return json.dumps({
        "success": True,
        "source": store,
        "targetUrl": f"https://{store}.com.br/produto/123",
        "scrapedProduct": {
            "success": True,
            "price": price,
            "name": f"Fralda {store}",
            "imageUrl": f"https://{store}.com.br/img/123.jpg",
            "originalPrice": None,
            "availability": True,
            "store": store,
            "scrapedAt": "2026-05-14T12:00:00.000Z",
        },
        "scrapedAt": "2026-05-14T12:00:00.000Z",
    })


def _fail_payload(store: str = "drogasil", error: str = "price not found") -> str:
    return json.dumps({
        "success": False,
        "source": store,
        "targetUrl": f"https://{store}.com.br/produto/123",
        "error": error,
        "scrapedAt": "2026-05-14T12:00:00.000Z",
    })


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _collector(db, *, timeout: int = 10, retries: int = 1, backoff: float = 0.0) -> PoupiLegacyRawCollector:
    return PoupiLegacyRawCollector(
        db,
        backend_dir=Path("/fake/backend"),
        timeout_seconds=timeout,
        retry_attempts=retries,
        retry_backoff_seconds=backoff,
    )


# ---------------------------------------------------------------------------
# Tests: _run_legacy_scraper
# ---------------------------------------------------------------------------

def test_run_legacy_scraper_returns_parsed_payload(db_session):
    collector = _collector(db_session)
    with patch("subprocess.run", return_value=_completed(_ok_payload())):
        payload = collector._run_legacy_scraper("https://drogasil.com.br/produto/123", "drogasil")

    assert payload["success"] is True
    assert payload["scrapedProduct"]["price"] == 49.9


def test_run_legacy_scraper_returns_failure_payload_without_raising(db_session):
    """success=False in JSON is valid — collector persists it, normalizer skips it."""
    collector = _collector(db_session)
    with patch("subprocess.run", return_value=_completed(_fail_payload())):
        payload = collector._run_legacy_scraper("https://drogasil.com.br/produto/123", "drogasil")

    assert payload["success"] is False


def test_run_legacy_scraper_raises_on_nonzero_exit_with_no_stdout(db_session):
    collector = _collector(db_session)
    with patch("subprocess.run", return_value=_completed("", returncode=1, stderr="ENOENT")):
        with pytest.raises(RuntimeError, match="ENOENT"):
            collector._run_legacy_scraper("https://drogasil.com.br/produto/123", "drogasil")


def test_run_legacy_scraper_raises_on_invalid_json(db_session):
    collector = _collector(db_session)
    with patch("subprocess.run", return_value=_completed("not json")):
        with pytest.raises(RuntimeError, match="invalid JSON"):
            collector._run_legacy_scraper("https://drogasil.com.br/produto/123", "drogasil")


def test_run_legacy_scraper_raises_on_nonzero_exit_with_json_error(db_session):
    """Non-zero returncode + JSON stdout → raises using payload.error message."""
    collector = _collector(db_session)
    payload = _fail_payload(error="timeout exceeded")
    with patch("subprocess.run", return_value=_completed(payload, returncode=1)):
        with pytest.raises(RuntimeError, match="timeout exceeded"):
            collector._run_legacy_scraper("https://drogasil.com.br/produto/123", "drogasil")


# ---------------------------------------------------------------------------
# Tests: retry logic
# ---------------------------------------------------------------------------

def test_retries_on_failure_and_succeeds_on_second_attempt(db_session):
    collector = _collector(db_session, retries=2, backoff=0.0)
    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _completed("", returncode=1, stderr="transient error")
        return _completed(_ok_payload())

    with patch("subprocess.run", side_effect=side_effect):
        payload, attempts = collector._run_legacy_scraper_with_retries(
            "https://drogasil.com.br/produto/123", "drogasil"
        )

    assert payload["success"] is True
    assert attempts == 2
    assert call_count["n"] == 2


def test_raises_after_all_retries_exhausted(db_session):
    collector = _collector(db_session, retries=2, backoff=0.0)

    with patch("subprocess.run", return_value=_completed("", returncode=1, stderr="fatal")):
        with pytest.raises(RuntimeError, match="fatal"):
            collector._run_legacy_scraper_with_retries(
                "https://drogasil.com.br/produto/123", "drogasil"
            )


# ---------------------------------------------------------------------------
# Tests: collect_targets
# ---------------------------------------------------------------------------

def test_collect_targets_saves_raw_records_on_success(db_session):
    collector = _collector(db_session)
    targets = [
        LegacyPoupiTarget(url="https://drogasil.com.br/produto/1", source_name="drogasil"),
        LegacyPoupiTarget(url="https://drogaraia.com.br/produto/2", source_name="drogaraia"),
    ]

    with patch("subprocess.run", side_effect=[
        _completed(_ok_payload("drogasil", 49.9)),
        _completed(_ok_payload("drogaraia", 55.0)),
    ]):
        result = collector.collect_targets(targets)

    assert result["raw_saved_count"] == 2
    assert result["error_count"] == 0

    rows = (
        db_session.query(RawCollection)
        .filter(RawCollection.source_name.in_(["drogasil", "drogaraia"]))
        .all()
    )
    assert len(rows) == 2


def test_collect_targets_counts_errors_individually(db_session):
    collector = _collector(db_session, retries=1)
    targets = [
        LegacyPoupiTarget(url="https://drogasil.com.br/produto/error-1", source_name="drogasil"),
        LegacyPoupiTarget(url="https://nissei.com.br/produto/2", source_name="nissei"),
    ]

    with patch("subprocess.run", side_effect=[
        _completed(_ok_payload("drogasil")),          # first succeeds
        _completed("", returncode=1, stderr="crash"),  # second fails
    ]):
        result = collector.collect_targets(targets)

    assert result["raw_saved_count"] == 1
    assert result["error_count"] == 1


def test_collect_targets_deduplicates_identical_payload(db_session):
    """Saving the same payload twice for the same URL should not raise."""
    collector = _collector(db_session)
    target = LegacyPoupiTarget(url="https://drogasil.com.br/produto/1", source_name="drogasil")

    payload = _ok_payload("drogasil")
    with patch("subprocess.run", return_value=_completed(payload)):
        result1 = collector.collect_targets([target])

    with patch("subprocess.run", return_value=_completed(payload)):
        result2 = collector.collect_targets([target])

    assert result1["error_count"] == 0
    assert result2["error_count"] == 0


# ---------------------------------------------------------------------------
# Tests: command selection
# ---------------------------------------------------------------------------

def test_legacy_scraper_command_uses_compiled_js_when_present(tmp_path):
    compiled = tmp_path / "dist" / "src" / "crawler" / "scrapers"
    compiled.mkdir(parents=True)
    (compiled / "raw-bridge.js").touch()

    collector = PoupiLegacyRawCollector.__new__(PoupiLegacyRawCollector)
    collector.backend_dir = tmp_path

    cmd = collector._legacy_scraper_command("https://drogasil.com.br/produto/1", "drogasil")

    assert cmd[-1] == "drogasil"
    assert "raw-bridge.js" in cmd[-3]
    assert "node" in cmd[0].lower()


def test_legacy_scraper_command_uses_ts_node_when_no_compiled_js(tmp_path):
    collector = PoupiLegacyRawCollector.__new__(PoupiLegacyRawCollector)
    collector.backend_dir = tmp_path  # no dist/ dir

    cmd = collector._legacy_scraper_command("https://drogasil.com.br/produto/1", "drogasil")

    assert "ts-node" in cmd
    assert any("raw-bridge.ts" in item for item in cmd)


# ---------------------------------------------------------------------------
# Tests: URL → source name inference
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.drogasil.com.br/produto/123", "drogasil"),
    ("https://farmaciasnissei.com.br/produto/456", "farmaciasnissei"),
    ("https://ultrafarma.com.br/produto/789", "ultrafarma"),
    ("https://panvel.com/produto/abc", "panvel"),
])
def test_guess_source_name(url: str, expected: str):
    assert PoupiLegacyRawCollector._guess_source_name(url) == expected

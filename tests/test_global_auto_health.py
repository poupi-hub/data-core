"""Tests for GlobalAutoHealth aggregator."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.global_auto_health.aggregator import (
    _classify,
    _operational_score,
    _worst,
    run_global_auto_health,
)


# ── _classify ─────────────────────────────────────────────────────────────────

def test_classify_ok():
    assert _classify({"ok": True, "status_code": 200, "body": {"status": "ok"}}) == "READY"


def test_classify_ready():
    assert _classify({"ok": True, "status_code": 200, "body": {"status": "ready"}}) == "READY"


def test_classify_degraded():
    assert _classify({"ok": True, "status_code": 200, "body": {"status": "degraded"}}) == "DEGRADED"


def test_classify_blocked_503():
    assert _classify({"ok": False, "status_code": 503, "error": "conn refused"}) == "BLOCKED"


def test_classify_blocked_no_conn():
    assert _classify({"ok": False, "status_code": None, "error": "timeout"}) == "BLOCKED"


def test_classify_200_unknown_status():
    # 200 but unrecognised status field → DEGRADED, not BLOCKED
    assert _classify({"ok": True, "status_code": 200, "body": {"status": "unknown"}}) == "DEGRADED"


# ── _worst ────────────────────────────────────────────────────────────────────

def test_worst_ignores_archived():
    assert _worst(["READY", "ARCHIVED"]) == "READY"


def test_worst_escalates():
    assert _worst(["READY", "DEGRADED", "INVESTIGAR"]) == "INVESTIGAR"


def test_worst_blocked_wins():
    assert _worst(["READY", "BLOCKED", "INVESTIGAR"]) == "BLOCKED"


def test_worst_all_archived():
    assert _worst(["ARCHIVED"]) == "READY"


# ── _operational_score ────────────────────────────────────────────────────────

def test_score_all_ready():
    components = {"a": {"status": "READY"}, "b": {"status": "READY"}}
    assert _operational_score(components) == 100


def test_score_with_blocked():
    components = {"a": {"status": "READY"}, "b": {"status": "BLOCKED"}}
    assert _operational_score(components) == 50


def test_score_ignores_archived():
    components = {"a": {"status": "READY"}, "sports": {"status": "ARCHIVED"}}
    assert _operational_score(components) == 100


def test_score_all_archived_returns_100():
    components = {"a": {"status": "ARCHIVED"}}
    assert _operational_score(components) == 100


# ── run_global_auto_health ────────────────────────────────────────────────────

def _mock_get_factory(responses: dict):
    """Return a _get mock that returns pre-defined responses keyed by URL fragment."""
    def _mock_get(url: str):
        for fragment, result in responses.items():
            if fragment in url:
                return result
        return {"ok": False, "status_code": None, "error": "unexpected url"}
    return _mock_get


def test_global_status_ready():
    ok = {"ok": True, "status_code": 200, "body": {"status": "ok"}}
    ready = {"ok": True, "status_code": 200, "body": {"status": "ready"}}
    orphan_ok = {"ok": True, "status_code": 200, "body": {"orphan_trades": 0, "status": "READY"}}
    price_ready = {"ok": True, "status_code": 200, "body": {"status": "ready"}}

    responses = {
        "localhost:8000/health": ok,
        "localhost:8000/ready": ready,
        "/health": ok,
        "/ready": ready,
        "orphan-trades": orphan_ok,
        "readiness/price-intelligence": price_ready,
    }

    with (
        patch("app.global_auto_health.aggregator._get", side_effect=_mock_get_factory(responses)),
        patch("app.global_auto_health.aggregator.settings") as mock_settings,
    ):
        mock_settings.poupi_crypto_internal_url = "http://crypto:8002"
        mock_settings.poupi_baby_url = "http://baby:3001"
        result = run_global_auto_health()

    assert result["status"] == "READY"
    assert result["schema_version"] == 1
    assert "generated_at" in result
    assert "operational_score" in result
    assert result["operational_score"] == 100
    assert result["components"]["sports"]["status"] == "ARCHIVED"


def test_global_status_blocked_when_data_core_down():
    blocked = {"ok": False, "status_code": 503, "error": "down"}

    with (
        patch("app.global_auto_health.aggregator._get", return_value=blocked),
        patch("app.global_auto_health.aggregator.settings") as mock_settings,
    ):
        mock_settings.poupi_crypto_internal_url = "http://crypto:8002"
        mock_settings.poupi_baby_url = "http://baby:3001"
        result = run_global_auto_health()

    assert result["status"] == "BLOCKED"


def test_global_status_investigar_when_orphan_trades():
    ok = {"ok": True, "status_code": 200, "body": {"status": "ok"}}
    ready = {"ok": True, "status_code": 200, "body": {"status": "ready", "safety": {}}}
    orphan = {"ok": True, "status_code": 200, "body": {"orphan_trades": 2, "status": "INVESTIGAR"}}

    def _mock(url):
        if "orphan-trades" in url:
            return orphan
        if "/ready" in url:
            return ready
        return ok

    with (
        patch("app.global_auto_health.aggregator._get", side_effect=_mock),
        patch("app.global_auto_health.aggregator.settings") as mock_settings,
    ):
        mock_settings.poupi_crypto_internal_url = "http://crypto:8002"
        mock_settings.poupi_baby_url = "http://baby:3001"
        result = run_global_auto_health()

    assert result["status"] == "INVESTIGAR"
    assert result["components"]["poupi-crypto"]["orphan_trades"]["orphan_trades"] == 2


def test_degraded_when_crypto_url_missing():
    ok = {"ok": True, "status_code": 200, "body": {"status": "ok"}}

    with (
        patch("app.global_auto_health.aggregator._get", return_value=ok),
        patch("app.global_auto_health.aggregator.settings") as mock_settings,
    ):
        mock_settings.poupi_crypto_internal_url = ""
        mock_settings.poupi_baby_url = ""
        result = run_global_auto_health()

    assert result["components"]["poupi-crypto"]["status"] == "DEGRADED"
    assert result["components"]["poupi-baby"]["status"] == "DEGRADED"

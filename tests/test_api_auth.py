"""
test_api_auth.py

Unit tests for the API key authentication middleware.
Uses monkeypatch to toggle settings without touching env vars.
"""

import pytest
from fastapi import HTTPException

from api.auth import verify_api_key
from core.config import settings


def _call(api_key: str | None) -> None:
    """Invoke verify_api_key synchronously (it's a plain function, not async)."""
    verify_api_key(api_key)


# ── Auth disabled (default dev mode) ─────────────────────────────────────────

def test_no_auth_required_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", False)
    _call(None)   # no exception
    _call("anything")   # no exception


# ── Auth enabled ──────────────────────────────────────────────────────────────

def test_valid_key_passes(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", True)
    monkeypatch.setattr(settings, "api_key", "super-secret-key")
    _call("super-secret-key")   # no exception


def test_missing_key_raises_401(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", True)
    monkeypatch.setattr(settings, "api_key", "super-secret-key")
    with pytest.raises(HTTPException) as exc_info:
        _call(None)
    assert exc_info.value.status_code == 401


def test_wrong_key_raises_401(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", True)
    monkeypatch.setattr(settings, "api_key", "super-secret-key")
    with pytest.raises(HTTPException) as exc_info:
        _call("wrong-key")
    assert exc_info.value.status_code == 401


def test_empty_string_key_raises_401(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", True)
    monkeypatch.setattr(settings, "api_key", "super-secret-key")
    with pytest.raises(HTTPException) as exc_info:
        _call("")
    assert exc_info.value.status_code == 401


def test_error_response_contains_www_authenticate_header(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", True)
    monkeypatch.setattr(settings, "api_key", "key")
    with pytest.raises(HTTPException) as exc_info:
        _call(None)
    assert "WWW-Authenticate" in exc_info.value.headers
    assert exc_info.value.headers["WWW-Authenticate"] == "ApiKey"

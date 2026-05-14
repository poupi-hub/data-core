from starlette.requests import Request

from api.rate_limit import rate_limit_key_func
from core.config import settings


def _request(headers: dict[str, str] | None = None, host: str = "203.0.113.10") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
            "client": (host, 12345),
        }
    )


def test_rate_limit_uses_api_key_bucket_when_api_key_auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", True)

    key_a = rate_limit_key_func(_request({"X-API-Key": "prod-key"}))
    key_b = rate_limit_key_func(_request({"X-API-Key": "staging-key"}))

    assert key_a.startswith("api-key:")
    assert key_b.startswith("api-key:")
    assert key_a != key_b
    assert "prod-key" not in key_a


def test_rate_limit_falls_back_to_ip_when_api_key_auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "api_key_enabled", False)

    assert rate_limit_key_func(_request({"X-API-Key": "prod-key"})) == "ip:203.0.113.10"

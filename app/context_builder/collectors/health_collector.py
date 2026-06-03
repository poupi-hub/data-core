"""
HealthCollector — consulta endpoints /health dos serviços afetados.

Mapeamento service → URL:
  data-core       → http://localhost:8000/health
  poupi-crypto    → http://localhost:8002/health  (ou poupi_crypto_internal_url)
  poupi-baby      → http://localhost:3001/health
"""

from __future__ import annotations

import urllib.request
import urllib.error
import json
from typing import Any

from app.context_builder.collectors.base import BaseCollector
from core.config import settings

_SERVICE_HEALTH_URLS: dict[str, str] = {
    "data-core":    "http://localhost:8000/health",
    "poupi-crypto": f"{settings.poupi_crypto_internal_url}/health",
    "poupi-baby":   f"{settings.poupi_baby_url}/health" if settings.poupi_baby_url else "http://localhost:3001/health",
}

# Endpoints de health extras por service
_EXTRA_HEALTH_URLS: dict[str, list[str]] = {
    "data-core": [
        "http://localhost:8000/api/v1/runtime/scheduler-diagnosis",
        "http://localhost:8000/health/business",
    ],
    "poupi-crypto": [
        f"{settings.poupi_crypto_internal_url}/api/v1/crypto/health/operational",
    ],
}


class HealthCollector(BaseCollector):
    name = "health"
    timeout_seconds = 8.0

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        service = context.get("service", "")
        results: dict[str, Any] = {}
        errors: list[str] = []

        # Coletar health do serviço afetado
        urls_to_check = []
        if service in _SERVICE_HEALTH_URLS:
            urls_to_check.append(("primary", _SERVICE_HEALTH_URLS[service]))
        for extra_name, extra_url in [
            (f"extra_{i}", u)
            for i, u in enumerate(_EXTRA_HEALTH_URLS.get(service, []))
        ]:
            urls_to_check.append((extra_name, extra_url))

        for endpoint_name, url in urls_to_check:
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    body = resp.read(4096).decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(body)
                        results[endpoint_name] = {
                            "url": url,
                            "status_code": resp.status,
                            "body": parsed,
                        }
                    except json.JSONDecodeError:
                        results[endpoint_name] = {
                            "url": url,
                            "status_code": resp.status,
                            "body_raw": body[:500],
                        }
            except urllib.error.HTTPError as exc:
                results[endpoint_name] = {
                    "url": url,
                    "status_code": exc.code,
                    "error": str(exc.reason),
                }
            except Exception as exc:
                errors.append(f"{endpoint_name} ({url}): {exc}")

        return {
            "service": service,
            "endpoints_checked": len(urls_to_check),
            "results": results,
            "errors": errors,
            "service_reachable": any(
                r.get("status_code", 0) < 500
                for r in results.values()
                if isinstance(r, dict)
            ),
        }

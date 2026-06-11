"""GlobalAutoHealth aggregator.

Coleta status de todos os serviços ativos chamando seus endpoints de health já existentes.
Não duplica checks internos — reutiliza o que cada serviço já expõe.

Serviços:
  data-core   → /health + /ready  (localhost:8000)
  poupi-crypto → /health + /ready + /api/v1/crypto/health/orphan-trades
  poupi-baby  → /health + /readiness/price-intelligence

Status por componente:
  READY | DEGRADED | INVESTIGAR | BLOCKED | ARCHIVED
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0

_STATUS_PRIORITY: dict[str, int] = {
    "BLOCKED": 4,
    "INVESTIGAR": 3,
    "DEGRADED": 2,
    "READY": 1,
    "ARCHIVED": 0,
}


def _get(url: str) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read(16384).decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"_raw": body[:200]}
            return {"ok": True, "status_code": resp.status, "body": parsed}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status_code": exc.code, "error": str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "status_code": None, "error": str(exc)}


def _classify(result: dict[str, Any], *, key: str = "status") -> str:
    """Derive component status from a single HTTP result."""
    if not result["ok"]:
        code = result.get("status_code")
        # 503 = service explicitly not ready
        return "BLOCKED" if (code is None or code >= 500) else "DEGRADED"
    body = result.get("body", {})
    raw = str(body.get(key, "")).lower()
    if raw in ("ok", "healthy", "ready", "alive"):
        return "READY"
    if raw in ("degraded", "partial"):
        return "DEGRADED"
    if raw in ("critical", "no-go", "not_ready", "blocked"):
        return "BLOCKED"
    if result["status_code"] == 200:
        return "DEGRADED"  # 200 but unrecognised status field
    return "BLOCKED"


def _worst(statuses: list[str]) -> str:
    active = [s for s in statuses if s != "ARCHIVED"]
    if not active:
        return "READY"
    return max(active, key=lambda s: _STATUS_PRIORITY.get(s, 0))


# ─────────────────────────────────────────────────────────────────────────────
# Per-service checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_data_core() -> dict[str, Any]:
    h = _get("http://localhost:8000/health")
    r = _get("http://localhost:8000/ready")
    s_h = _classify(h)
    s_r = _classify(r)
    status = _worst([s_h, s_r])
    return {
        "status": status,
        "health": h.get("body", {}).get("status"),
        "ready": r.get("body", {}).get("status"),
    }


def _check_poupi_crypto() -> dict[str, Any]:
    url = settings.poupi_crypto_internal_url
    if not url:
        return {"status": "DEGRADED", "detail": "POUPI_CRYPTO_INTERNAL_URL not configured"}

    h = _get(f"{url}/health")
    r = _get(f"{url}/ready")
    s_h = _classify(h)
    s_r = _classify(r)

    safety: dict[str, Any] = {}
    orphans: dict[str, Any] = {}

    # Safety flags exposed by /ready
    if r["ok"]:
        body = r.get("body", {})
        safety = body.get("safety", {})

    # Orphan real trades
    orphan_result = _get(f"{url}/api/v1/crypto/health/orphan-trades")
    if orphan_result["ok"]:
        orphans = orphan_result.get("body", {})
    else:
        orphans = {"status": "NOT_CHECKED", "error": orphan_result.get("error", "unreachable")}

    orphan_status = orphans.get("status", "NOT_CHECKED")
    status = _worst([s_h, s_r, orphan_status if orphan_status in _STATUS_PRIORITY else "READY"])

    return {
        "status": status,
        "health": h.get("body", {}).get("status"),
        "ready": r.get("body", {}).get("status"),
        "safety": safety,
        "orphan_trades": orphans,
        "url": url,
    }


def _check_poupi_baby() -> dict[str, Any]:
    url = settings.poupi_baby_url
    if not url:
        return {"status": "DEGRADED", "detail": "POUPI_BABY_URL not configured"}

    h = _get(f"{url}/health")
    ri = _get(f"{url}/readiness/price-intelligence")
    s_h = _classify(h)
    s_r = _classify(ri)
    status = _worst([s_h, s_r])
    return {
        "status": status,
        "health": h.get("body", {}).get("status"),
        "readiness": ri.get("body", {}).get("status") if ri["ok"] else "unreachable",
        "url": url,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

_SCORE: dict[str, int] = {
    "READY": 100,
    "DEGRADED": 60,
    "INVESTIGAR": 30,
    "BLOCKED": 0,
    "ARCHIVED": -1,  # excluded from score
}


def _operational_score(components: dict[str, Any]) -> int:
    scores = [
        _SCORE[c["status"]]
        for c in components.values()
        if _SCORE.get(c["status"], -1) >= 0
    ]
    if not scores:
        return 100
    return round(sum(scores) / len(scores))


def run_global_auto_health() -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    components: dict[str, Any] = {
        "data-core": _check_data_core(),
        "poupi-crypto": _check_poupi_crypto(),
        "poupi-baby": _check_poupi_baby(),
        "sports": {
            "status": "ARCHIVED",
            "detail": "NBA/WNBA parked — reactivate via ENABLE_SPORTS=true",
        },
    }

    global_status = _worst([c["status"] for c in components.values()])

    return {
        "schema_version": 1,
        "status": global_status,
        "operational_score": _operational_score(components),
        "generated_at": now.isoformat(),
        "components": components,
    }

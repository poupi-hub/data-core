"""Read-only smoke test for Scheduler Reliability dry-run calibration.

The script validates local audit evidence and, when a base URL is supplied,
checks the runtime endpoint and Prometheus exposition. It never changes env
vars, never starts or stops services, and never calls endpoints that create
new reliability decisions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime.scheduler_reliability import (
    AUDIT_PATH,
    scheduler_reliability_audit_report,
)

REQUIRED_DASHBOARD_PANELS = {
    "Dry-run decisions timeline",
    "Mode changes",
    "Max observed pressure",
    "Activation readiness panel",
}

REQUIRED_METRICS = {
    "reliability_dry_run_decisions_total",
    "reliability_mode_changes_total",
    "reliability_false_positive_candidates_total",
    "reliability_max_memory_ratio_observed",
    "reliability_max_backlog_score_observed",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scheduler reliability dry-run smoke test")
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional API base URL, e.g. http://localhost:8000",
    )
    parser.add_argument("--api-key", default="", help="Optional X-API-Key for protected API routes")
    parser.add_argument("--audit-path", default=str(AUDIT_PATH))
    parser.add_argument(
        "--dashboard",
        default="grafana/dashboards/data_core_scheduler_runtime.json",
    )
    parser.add_argument("--last-minutes", type=int, default=None)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when readiness gates fail",
    )
    args = parser.parse_args()

    audit_path = Path(args.audit_path)
    report = scheduler_reliability_audit_report(audit_path, last_minutes=args.last_minutes)
    dashboard = validate_dashboard(Path(args.dashboard))
    endpoint = check_endpoint(args.base_url, args.api_key, args.last_minutes)
    metrics = check_metrics(args.base_url)

    payload = {
        "audit": {
            "path": str(audit_path),
            "directory_exists": audit_path.parent.exists(),
            "file_exists": audit_path.exists(),
            "file_size_bytes": audit_path.stat().st_size if audit_path.exists() else 0,
            "health": report["audit_health"],
            "summary": report["summary"],
            "activation_gates": report["activation_gates"],
        },
        "endpoint": endpoint,
        "metrics": metrics,
        "dashboard": dashboard,
        "recommendation": report["summary"]["readiness_recommendation"],
        "read_only": True,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    if args.strict and payload["recommendation"] != "READY_FOR_LIMITED_ENABLEMENT":
        return 2
    if args.strict and not dashboard["valid"]:
        return 3
    return 0


def validate_dashboard(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "valid": False, "error": "dashboard_not_found"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"path": str(path), "valid": False, "error": f"invalid_json:{exc}"}

    titles = {panel.get("title") for panel in data.get("panels", [])}
    missing = sorted(REQUIRED_DASHBOARD_PANELS - titles)
    return {
        "path": str(path),
        "valid": not missing,
        "missing_panels": missing,
        "panel_count": len(data.get("panels", [])),
    }


def check_endpoint(base_url: str, api_key: str, last_minutes: int | None) -> dict[str, Any]:
    if not base_url:
        return {"checked": False, "reason": "base_url_not_provided"}
    suffix = "/api/v1/runtime/scheduler-reliability-audit"
    if last_minutes is not None:
        suffix += f"?last_minutes={last_minutes}"
    url = base_url.rstrip("/") + suffix
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        body = _http_get(url, headers=headers)
        data = json.loads(body)
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"checked": True, "ok": False, "url": url, "error": str(exc)}
    return {
        "checked": True,
        "ok": "summary" in data and "operational_report" in data,
        "url": url,
        "recommendation": data.get("summary", {}).get("readiness_recommendation"),
    }


def check_metrics(base_url: str) -> dict[str, Any]:
    if not base_url:
        return {"checked": False, "reason": "base_url_not_provided"}
    url = base_url.rstrip("/") + "/metrics"
    try:
        body = _http_get(url)
    except (URLError, TimeoutError) as exc:
        return {"checked": True, "ok": False, "url": url, "error": str(exc)}
    missing = sorted(metric for metric in REQUIRED_METRICS if metric not in body)
    return {"checked": True, "ok": not missing, "url": url, "missing_metrics": missing}


def _http_get(url: str, headers: dict[str, str] | None = None) -> str:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

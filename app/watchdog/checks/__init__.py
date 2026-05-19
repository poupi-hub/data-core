"""Watchdog check modules — each returns a CheckResult."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WatchdogAlert:
    """A single actionable alert produced by a check."""

    severity: str          # "critical" | "warning"
    code: str              # machine-readable code, e.g. "no_collection_3h"
    title: str             # short title for Telegram message
    message: str           # full human-readable message
    source_name: str | None = None  # domain / source the alert relates to
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    """Result of a single watchdog check category."""

    name: str               # "collection" | "normalization" | "scraper_quality" | "telegram"
    status: str             # "ok" | "warning" | "critical"
    summary: str            # one-line human-readable summary
    alerts: list[WatchdogAlert] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "alerts": [
                {
                    "severity": a.severity,
                    "code": a.code,
                    "title": a.title,
                    "message": a.message,
                    "source_name": a.source_name,
                    "context": a.context,
                }
                for a in self.alerts
            ],
            "metrics": self.metrics,
        }

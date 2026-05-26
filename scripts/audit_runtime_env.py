#!/usr/bin/env python3
"""Env Drift Detection — Phase 7.

Detects divergence between .env.example, runtime environment, and known
critical configuration values that cause silent pipeline failures.

Exit codes:
  0 — No drift detected
  1 — CRITICAL drift (at least one dangerous misconfiguration found)
  2 — WARNING drift (missing or suspicious values)

Usage::

    python scripts/audit_runtime_env.py
    python scripts/audit_runtime_env.py --env-file .env
    python scripts/audit_runtime_env.py --json          # machine-readable output
    python scripts/audit_runtime_env.py --strict        # treat warnings as errors

Root cause this script prevents:
  SCHEDULER_PIPELINE_ENABLED=false  →  normalize_job never runs
  →  normalized_products stays empty
  →  /price-feed count=0 forever
  →  poupi-baby readiness shows feed.status=degraded

The same silent failure can be caused by:
  - Coolify overwriting env vars on deploy
  - Typo in compose env block
  - Missing secret in deployment platform
  - Env var present in .env but missing from docker-compose.prod.yml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# Critical variable rules
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class EnvRule:
    key: str
    severity: str        # "critical" | "warning" | "info"
    expected: str | None = None        # exact expected value (or None = just must be present)
    not_value: str | None = None       # value that is WRONG (e.g. "false" for ENABLED flags)
    must_be_set: bool = True
    description: str = ""
    remediation: str = ""


CRITICAL_RULES: list[EnvRule] = [
    # ── The root cause we fixed ───────────────────────────────────────────────
    EnvRule(
        key="SCHEDULER_PIPELINE_ENABLED",
        severity="critical",
        not_value="false",
        description=(
            "SCHEDULER_PIPELINE_ENABLED=false disables normalize_job and analytics_job. "
            "Result: normalized_products stays empty; /price-feed returns count=0 forever."
        ),
        remediation="Set SCHEDULER_PIPELINE_ENABLED=true in your runtime env and redeploy.",
    ),
    EnvRule(
        key="SCHEDULER_ENABLED",
        severity="critical",
        not_value="false",
        description="SCHEDULER_ENABLED=false disables ALL scheduled jobs — no collection, no normalization.",
        remediation="Set SCHEDULER_ENABLED=true in the scheduler service environment.",
    ),
    # ── DB / Redis — must be set ──────────────────────────────────────────────
    EnvRule(
        key="DATABASE_URL",
        severity="critical",
        must_be_set=True,
        description="Database URL must be set for all services.",
        remediation="Set DATABASE_URL=postgresql+psycopg://user:pass@host:port/db",
    ),
    EnvRule(
        key="DATABASE_URL",
        severity="critical",
        not_value="postgresql+psycopg://data_core:data_core@localhost:5432/data_core",
        description="DATABASE_URL is set to the development default — will fail in production.",
        remediation="Override DATABASE_URL with production credentials.",
    ),
    # ── Collectors ────────────────────────────────────────────────────────────
    EnvRule(
        key="SCHEDULER_COLLECTORS_ENABLED",
        severity="warning",
        not_value="false",
        description=(
            "SCHEDULER_COLLECTORS_ENABLED=false disables all collector jobs — "
            "raw_collections will not receive new data."
        ),
        remediation="Set SCHEDULER_COLLECTORS_ENABLED=true unless intentionally pausing collection.",
    ),
    EnvRule(
        key="SCHEDULER_DOMAIN_JOBS_ENABLED",
        severity="warning",
        not_value="false",
        description=(
            "SCHEDULER_DOMAIN_JOBS_ENABLED=false disables run_ecommerce_url_targets_job — "
            "VTEX scraping will not run."
        ),
        remediation="Set SCHEDULER_DOMAIN_JOBS_ENABLED=true in production.",
    ),
    # ── API security ──────────────────────────────────────────────────────────
    EnvRule(
        key="API_KEY",
        severity="warning",
        not_value="change-me",
        description="API_KEY is set to the insecure placeholder 'change-me'.",
        remediation="Set API_KEY to a strong random secret.",
    ),
    EnvRule(
        key="REDIS_URL",
        severity="warning",
        not_value="redis://:change-me@redis:6379/2",
        description="REDIS_URL still uses the 'change-me' placeholder password.",
        remediation="Set REDIS_URL with a real password.",
    ),
    # ── Watchdog ─────────────────────────────────────────────────────────────
    EnvRule(
        key="WATCHDOG_ENABLED",
        severity="warning",
        not_value="false",
        description="WATCHDOG_ENABLED=false disables operational watchdog and Telegram alerts.",
        remediation="Set WATCHDOG_ENABLED=true in production.",
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# Drift result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DriftFinding:
    key: str
    severity: str
    description: str
    remediation: str
    runtime_value: str | None


@dataclass
class EnvDriftReport:
    evaluated_at: str
    env_file: str | None
    findings: list[DriftFinding] = field(default_factory=list)
    example_keys: list[str] = field(default_factory=list)
    missing_from_runtime: list[str] = field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "findings": [asdict(f) for f in self.findings],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Auditor
# ──────────────────────────────────────────────────────────────────────────────

class EnvDriftAuditor:
    def __init__(self, env_file: str | None = None) -> None:
        self.env_file = env_file
        self._runtime = {**os.environ}  # snapshot at startup

    def audit(self) -> EnvDriftReport:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()

        example_keys = self._parse_example_file()
        missing = self._find_missing_from_runtime(example_keys)

        report = EnvDriftReport(
            evaluated_at=now_iso,
            env_file=self.env_file,
            example_keys=example_keys,
            missing_from_runtime=missing,
        )

        # Apply explicit rules
        for rule in CRITICAL_RULES:
            finding = self._apply_rule(rule)
            if finding:
                report.findings.append(finding)
                if finding.severity == "critical":
                    report.critical_count += 1
                    report.ok = False
                elif finding.severity == "warning":
                    report.warning_count += 1
                else:
                    report.info_count += 1

        # Missing keys from .env.example that are in CRITICAL_RULES
        for key in missing:
            rule = next((r for r in CRITICAL_RULES if r.key == key), None)
            if rule and rule.severity == "critical":
                report.ok = False

        return report

    def _apply_rule(self, rule: EnvRule) -> DriftFinding | None:
        """Return a finding if the rule is violated, else None."""
        runtime_value = self._runtime.get(rule.key)

        if rule.must_be_set and runtime_value is None:
            return DriftFinding(
                key=rule.key,
                severity=rule.severity,
                description=f"{rule.key} is not set in runtime env. {rule.description}",
                remediation=rule.remediation,
                runtime_value=None,
            )

        if rule.not_value is not None and runtime_value is not None:
            if runtime_value.strip().lower() == rule.not_value.strip().lower():
                return DriftFinding(
                    key=rule.key,
                    severity=rule.severity,
                    description=rule.description,
                    remediation=rule.remediation,
                    runtime_value=runtime_value,
                )

        if rule.expected is not None and runtime_value is not None:
            if runtime_value.strip() != rule.expected.strip():
                return DriftFinding(
                    key=rule.key,
                    severity=rule.severity,
                    description=(
                        f"{rule.key} expected={rule.expected!r} "
                        f"got={runtime_value!r}. {rule.description}"
                    ),
                    remediation=rule.remediation,
                    runtime_value=runtime_value,
                )

        return None

    def _parse_example_file(self) -> list[str]:
        """Parse .env.example (or the provided file) and return a list of key names."""
        if self.env_file is None:
            # Search common locations
            candidates = [
                Path(".env.example"),
                Path("infra/projects/data-core/.env.example"),
                Path("../.env.example"),
            ]
            env_path = next((p for p in candidates if p.exists()), None)
        else:
            env_path = Path(self.env_file)

        if env_path is None or not env_path.exists():
            return []

        self.env_file = str(env_path)
        keys = []
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
                if match:
                    keys.append(match.group(1))
        except Exception:
            pass
        return keys

    def _find_missing_from_runtime(self, example_keys: list[str]) -> list[str]:
        """Keys present in .env.example but absent in runtime env."""
        return [k for k in example_keys if k not in self._runtime]


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _out(text: str) -> None:
    """Print with safe encoding fallback for Windows cp1252 terminals."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _print_report(report: EnvDriftReport, use_json: bool) -> None:
    if use_json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    SEP = "-" * 70
    _out(SEP)
    _out(f"  ENV DRIFT AUDIT  [{report.evaluated_at}]")
    _out(SEP)
    if report.env_file:
        _out(f"  .env.example : {report.env_file}")
    _out(f"  Example keys : {len(report.example_keys)}")
    _out(f"  Missing keys : {len(report.missing_from_runtime)}")
    _out(f"  Critical     : {report.critical_count}")
    _out(f"  Warnings     : {report.warning_count}")
    _out(SEP)

    if not report.findings and not report.missing_from_runtime:
        _out("  [OK] No drift detected -- runtime env matches expected configuration.")
        _out(SEP)
        return

    for f in report.findings:
        icon = "[CRITICAL]" if f.severity == "critical" else "[WARNING]" if f.severity == "warning" else "[INFO]"
        _out(f"\n  {icon} {f.key}")
        _out(f"     {f.description}")
        if f.runtime_value is not None:
            _out(f"     Current value : {f.runtime_value!r}")
        _out(f"     Remediation   : {f.remediation}")

    if report.missing_from_runtime:
        _out(f"\n  [INFO] Missing from runtime (present in .env.example):")
        for k in report.missing_from_runtime:
            _out(f"       {k}")

    _out(f"\n{SEP}")
    if report.ok:
        _out("  [WARNING] Warnings only -- runtime can continue but review recommended.")
    else:
        _out("  [CRITICAL] CRITICAL drift detected -- pipeline reliability at risk.")
    _out(SEP)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit runtime environment for critical drift vs .env.example"
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env.example (default: auto-detect)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Treat warnings as errors (exit 1)",
    )
    args = parser.parse_args()

    auditor = EnvDriftAuditor(env_file=args.env_file)
    report = auditor.audit()

    _print_report(report, use_json=args.json)

    if not report.ok:
        return 1
    if args.strict and report.warning_count > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

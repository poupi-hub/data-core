"""DiagnosticsEngine — rule-based auto-diagnostics for scraper health.

Produces actionable diagnostic messages from aggregated signals:
  - recent drift events
  - fallback rate (how often we fell back to lower-trust strategies)
  - anti-bot detection counts
  - payload quality scores
  - scraper enabled/disabled state

Diagnostic codes
────────────────
  drift_detected          One or more high/critical drift events in last window
  fallback_excessive      > threshold% of recent payloads used fallback strategies
  anti_bot_growing        Anti-bot detection rate > threshold per hour
  payload_quality_low     Average quality score < threshold
  scraper_disabled        Scraper domain is auto-disabled (< 20% success rate)
  ok                      No issues detected
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DiagnosticResult:
    code: str  # "ok" | one of the codes above
    severity: str  # "ok" | "warning" | "error" | "critical"
    title: str
    description: str
    recommended_action: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "recommended_action": self.recommended_action,
            "context": self.context,
        }


class DiagnosticsEngine:
    """Evaluate health signals for a scraper domain and return diagnostics.

    Usage::

        engine = DiagnosticsEngine()
        results = engine.evaluate(
            source_name="drogasil",
            drift_events=[...],           # List[dict] with drift_type/risk_level
            fallback_count=3,
            total_count=10,
            anti_bot_count=2,
            window_hours=1,
            avg_quality_score=72,
            scraper_enabled=True,
        )
        for r in results:
            print(r.code, r.severity, r.description)
    """

    # Thresholds (overridable via subclass or constructor)
    FALLBACK_RATE_THRESHOLD = 0.40        # 40% fallback rate triggers warning
    ANTI_BOT_RATE_THRESHOLD = 2           # >2 detections/hour triggers warning
    QUALITY_SCORE_THRESHOLD = 50          # avg quality < 50 triggers warning
    CRITICAL_RISK_LEVELS = frozenset({"critical", "high"})

    def evaluate(
        self,
        source_name: str,
        drift_events: list[dict[str, Any]] | None = None,
        fallback_count: int = 0,
        total_count: int = 0,
        anti_bot_count: int = 0,
        window_hours: float = 1.0,
        avg_quality_score: float | None = None,
        scraper_enabled: bool = True,
    ) -> list[DiagnosticResult]:
        """Return list of DiagnosticResult — empty means all OK."""
        results: list[DiagnosticResult] = []

        # ── Scraper disabled ──────────────────────────────────────────────────
        if not scraper_enabled:
            results.append(DiagnosticResult(
                code="scraper_disabled",
                severity="error",
                title=f"Scraper auto-disabled: {source_name}",
                description=(
                    f"The scraper for '{source_name}' has been auto-disabled by "
                    "ScraperHealthService due to low success rate (< 20%)."
                ),
                recommended_action=(
                    "Investigate the scraper logs for this domain. "
                    "Check for site changes, anti-bot blocks, or broken selectors. "
                    "Re-enable via admin API after fixing."
                ),
                context={"source_name": source_name},
            ))

        # ── Drift detected ─────────────────────────────────────────────────────
        if drift_events:
            critical_events = [
                e for e in drift_events
                if e.get("risk_level") in self.CRITICAL_RISK_LEVELS
            ]
            if critical_events:
                results.append(DiagnosticResult(
                    code="drift_detected",
                    severity="critical" if any(
                        e.get("risk_level") == "critical" for e in critical_events
                    ) else "error",
                    title=f"Structural drift detected: {source_name}",
                    description=(
                        f"{len(critical_events)} high/critical drift event(s) detected for "
                        f"'{source_name}'. Payload schema may have changed."
                    ),
                    recommended_action=(
                        "Review recent ScraperDriftEvent records. "
                        "Compare current payload against baseline. "
                        "Update scraper selectors or fallback strategy if site layout changed."
                    ),
                    context={
                        "source_name": source_name,
                        "drift_event_count": len(critical_events),
                        "drift_types": list({e.get("drift_type") for e in critical_events}),
                    },
                ))

        # ── Excessive fallback rate ───────────────────────────────────────────
        if total_count > 0:
            fallback_rate = fallback_count / total_count
            if fallback_rate > self.FALLBACK_RATE_THRESHOLD:
                results.append(DiagnosticResult(
                    code="fallback_excessive",
                    severity="warning",
                    title=f"High fallback rate: {source_name}",
                    description=(
                        f"{fallback_count}/{total_count} ({fallback_rate:.0%}) recent scrapes for "
                        f"'{source_name}' used a fallback strategy. "
                        "Primary strategy may be failing."
                    ),
                    recommended_action=(
                        "Check if the primary strategy (VTEX API or JSON-LD) is still working. "
                        "Inspect the raw payloads for 'scraper_strategy' field. "
                        "Update primary strategy if site layout changed."
                    ),
                    context={
                        "source_name": source_name,
                        "fallback_count": fallback_count,
                        "total_count": total_count,
                        "fallback_rate": round(fallback_rate, 3),
                    },
                ))

        # ── Anti-bot growing ──────────────────────────────────────────────────
        if window_hours > 0:
            rate_per_hour = anti_bot_count / window_hours
            if rate_per_hour > self.ANTI_BOT_RATE_THRESHOLD:
                results.append(DiagnosticResult(
                    code="anti_bot_growing",
                    severity="warning",
                    title=f"Anti-bot detections increasing: {source_name}",
                    description=(
                        f"{anti_bot_count} anti-bot detection(s) in {window_hours:.1f}h window "
                        f"({rate_per_hour:.1f}/h) for '{source_name}'."
                    ),
                    recommended_action=(
                        "Consider enabling proxy rotation or increasing request delays. "
                        "Review User-Agent headers and request fingerprint. "
                        "Check if site added new Cloudflare rules."
                    ),
                    context={
                        "source_name": source_name,
                        "anti_bot_count": anti_bot_count,
                        "window_hours": window_hours,
                        "rate_per_hour": round(rate_per_hour, 2),
                    },
                ))

        # ── Payload quality low ───────────────────────────────────────────────
        if avg_quality_score is not None and avg_quality_score < self.QUALITY_SCORE_THRESHOLD:
            results.append(DiagnosticResult(
                code="payload_quality_low",
                severity="warning",
                title=f"Low payload quality: {source_name}",
                description=(
                    f"Average quality score for '{source_name}' is "
                    f"{avg_quality_score:.0f}/100 (threshold: {self.QUALITY_SCORE_THRESHOLD})."
                ),
                recommended_action=(
                    "Inspect recent raw payloads for missing fields (title, price, source_id). "
                    "Check if the product pages have changed structure. "
                    "Run scraper manually to see current payload content."
                ),
                context={
                    "source_name": source_name,
                    "avg_quality_score": avg_quality_score,
                    "threshold": self.QUALITY_SCORE_THRESHOLD,
                },
            ))

        return results

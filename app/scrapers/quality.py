"""PayloadQualityScorer — scores a scraped product payload from 0 to 100.

Scoring dimensions
──────────────────
+20  title present and non-trivial (>= 3 chars, not just whitespace)
+25  price present, > 0, and plausible (< 100 000 BRL)
+15  availability field is one of the recognised canonical values
+15  source_id present and non-empty
+10  scraper_strategy bonus — vtex_api > json_ld > meta_css > unknown
+15  latency bonus — 0 for >= 10 s, up to 15 for <= 0.5 s

Total maximum: 100

A quality_grade string is derived:
  90-100 → "excellent"
  70-89  → "good"
  50-69  → "fair"
  30-49  → "poor"
  0-29   → "critical"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


_STRATEGY_BONUS: dict[str, int] = {
    "vtex_api": 10,
    "json_ld": 8,
    "meta_css": 4,
    "ng_state": 9,
    "ssr": 9,
}

_KNOWN_AVAILABILITY: frozenset[str] = frozenset(
    {"in_stock", "out_of_stock", "preorder", "discontinued"}
)

_MAX_PRICE_BRL = 100_000.0


@dataclass
class QualityResult:
    score: int
    grade: str
    breakdown: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def is_acceptable(self) -> bool:
        return self.score >= 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "grade": self.grade,
            "is_acceptable": self.is_acceptable,
            "breakdown": self.breakdown,
            "issues": self.issues,
        }


class PayloadQualityScorer:
    """Score a scraped product dict (the inner scrapedProduct object).

    Usage::

        scorer = PayloadQualityScorer()
        result = scorer.score(scraped_product, latency_seconds=1.2)
        print(result.score, result.grade)
    """

    def score(
        self,
        product: dict[str, Any],
        latency_seconds: float | None = None,
    ) -> QualityResult:
        """Return a QualityResult for the given scraped product dict."""
        breakdown: dict[str, int] = {}
        issues: list[str] = []

        # ── Title (+20) ───────────────────────────────────────────────────────
        title = str(product.get("title") or "").strip()
        if len(title) >= 3:
            breakdown["title"] = 20
        else:
            breakdown["title"] = 0
            issues.append(f"title invalid or too short: {title!r}")

        # ── Price (+25) ───────────────────────────────────────────────────────
        price = product.get("price")
        if isinstance(price, (int, float)) and 0 < price < _MAX_PRICE_BRL:
            breakdown["price"] = 25
        elif price == 0 or price is None:
            breakdown["price"] = 0
            issues.append(f"price missing or zero: {price!r}")
        else:
            # Implausible value
            breakdown["price"] = 0
            issues.append(f"price out of range: {price!r}")

        # ── Availability (+15) ────────────────────────────────────────────────
        avail = str(product.get("availability") or "").strip().lower()
        if avail in _KNOWN_AVAILABILITY:
            breakdown["availability"] = 15
        else:
            breakdown["availability"] = 0
            issues.append(f"availability unknown: {avail!r}")

        # ── Source ID (+15) ───────────────────────────────────────────────────
        source_id = str(product.get("source_id") or "").strip()
        if source_id:
            breakdown["source_id"] = 15
        else:
            breakdown["source_id"] = 0
            issues.append("source_id missing")

        # ── Strategy bonus (+10) ──────────────────────────────────────────────
        strategy = str(product.get("scraper_strategy") or "unknown").strip().lower()
        breakdown["strategy"] = _STRATEGY_BONUS.get(strategy, 0)
        if breakdown["strategy"] == 0:
            issues.append(f"unknown strategy: {strategy!r}")

        # ── Latency bonus (+15) ───────────────────────────────────────────────
        if latency_seconds is not None:
            lat = float(latency_seconds)
            if lat <= 0.5:
                lat_pts = 15
            elif lat >= 10.0:
                lat_pts = 0
            else:
                # Linear decay between 0.5 s → 10 s
                lat_pts = int(15 * (1 - (lat - 0.5) / 9.5))
                lat_pts = max(0, min(15, lat_pts))
            breakdown["latency"] = lat_pts
        else:
            breakdown["latency"] = 0  # no latency info — neutral

        total = sum(breakdown.values())
        total = max(0, min(100, total))

        return QualityResult(
            score=total,
            grade=_grade(total),
            breakdown=breakdown,
            issues=issues,
        )


def _grade(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "fair"
    if score >= 30:
        return "poor"
    return "critical"

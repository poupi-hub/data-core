"""StructuralDriftDetector — detect payload schema drift across scrape runs.

What it detects
───────────────
For each new scraped product payload, the detector compares the set of
present fields (and their types) against a rolling baseline computed from
the last N successful payloads for that source.

Drift types
───────────
  field_missing         A previously-present field is now absent
  field_added           A new field appeared that wasn't in the baseline
  type_changed          A field's value type changed (e.g. price was float, now str)
  price_zero            Price is 0 or None when baseline had valid prices
  availability_unknown  Availability value not in canonical set
  strategy_fallback     Scraper fell back to a lower-confidence strategy

Risk levels
───────────
  critical  price_zero or price type_changed
  high      field_missing on required fields (price, title, availability)
  medium    field_missing on optional fields, or strategy_fallback
  low       field_added, cosmetic type changes

The detector is stateless — callers are responsible for storing
ScraperDriftEvent rows (see diagnostics.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Required fields whose absence is high/critical risk
_REQUIRED_FIELDS: frozenset[str] = frozenset({"title", "price", "availability", "store_name"})
_CRITICAL_FIELDS: frozenset[str] = frozenset({"price"})

_KNOWN_AVAILABILITY: frozenset[str] = frozenset(
    {"in_stock", "out_of_stock", "preorder", "discontinued"}
)

# Lower-trust strategies — signal possible site-change
_FALLBACK_STRATEGIES: frozenset[str] = frozenset({"meta_css", "og_meta", "unknown"})
_PRIMARY_STRATEGIES: frozenset[str] = frozenset({"vtex_api", "json_ld", "ng_state", "ssr"})


@dataclass
class DriftEvent:
    drift_type: str
    risk_level: str
    description: str
    field_name: str | None = None
    prev_signature: dict[str, Any] | None = None
    curr_signature: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "drift_type": self.drift_type,
            "risk_level": self.risk_level,
            "description": self.description,
            "field_name": self.field_name,
            "prev_signature": self.prev_signature,
            "curr_signature": self.curr_signature,
        }


class StructuralDriftDetector:
    """Compares a new payload against a historical baseline to detect drift.

    The baseline is a dict mapping field names → expected Python type name.
    It is built from the ``build_baseline`` helper or injected externally.

    Usage::

        detector = StructuralDriftDetector()
        baseline = detector.build_baseline(historical_payloads)
        events = detector.detect(new_payload, baseline, source_name="drogasil", prev_strategy="vtex_api")
        for ev in events:
            print(ev.drift_type, ev.risk_level, ev.description)
    """

    # ── Baseline building ──────────────────────────────────────────────────────

    def build_baseline(self, payloads: list[dict[str, Any]]) -> dict[str, str]:
        """Compute a majority-vote baseline from a list of scraped product dicts.

        Returns a mapping {field_name: most_common_type_name}.
        Fields that appear in < 50% of payloads are considered optional and
        NOT included in the baseline (absent fields won't trigger drift).
        """
        if not payloads:
            return {}

        counts: dict[str, dict[str, int]] = {}
        for payload in payloads:
            for k, v in payload.items():
                if k not in counts:
                    counts[k] = {}
                t = type(v).__name__
                counts[k][t] = counts[k].get(t, 0) + 1

        total = len(payloads)
        baseline: dict[str, str] = {}
        for fname, type_counts in counts.items():
            best_type, best_count = max(type_counts.items(), key=lambda x: x[1])
            if best_count / total >= 0.5:
                baseline[fname] = best_type

        return baseline

    # ── Detection ─────────────────────────────────────────────────────────────

    def detect(
        self,
        payload: dict[str, Any],
        baseline: dict[str, str],
        source_name: str = "",
        prev_strategy: str | None = None,
    ) -> list[DriftEvent]:
        """Return a list of DriftEvent for any detected anomalies.

        Empty list means no drift detected.
        """
        if not baseline:
            return []

        events: list[DriftEvent] = []

        # ── Field-level drift ─────────────────────────────────────────────────
        for fname, expected_type in baseline.items():
            if fname not in payload:
                # Missing field
                risk = "critical" if fname in _CRITICAL_FIELDS else (
                    "high" if fname in _REQUIRED_FIELDS else "medium"
                )
                events.append(DriftEvent(
                    drift_type="field_missing",
                    risk_level=risk,
                    description=f"Field '{fname}' missing from payload (baseline type: {expected_type})",
                    field_name=fname,
                    prev_signature={"field": fname, "type": expected_type},
                    curr_signature={"field": fname, "type": "absent"},
                ))
            else:
                actual_type = type(payload[fname]).__name__
                if actual_type != expected_type:
                    # Type change — critical for price
                    risk = "critical" if fname in _CRITICAL_FIELDS else "medium"
                    events.append(DriftEvent(
                        drift_type="type_changed",
                        risk_level=risk,
                        description=(
                            f"Field '{fname}' type changed: "
                            f"expected {expected_type!r}, got {actual_type!r}"
                        ),
                        field_name=fname,
                        prev_signature={"field": fname, "type": expected_type},
                        curr_signature={"field": fname, "type": actual_type, "value": repr(payload[fname])[:80]},
                    ))

        # ── New fields ────────────────────────────────────────────────────────
        for fname in payload:
            if fname not in baseline:
                events.append(DriftEvent(
                    drift_type="field_added",
                    risk_level="low",
                    description=f"New field '{fname}' appeared in payload (type: {type(payload[fname]).__name__})",
                    field_name=fname,
                    prev_signature=None,
                    curr_signature={"field": fname, "type": type(payload[fname]).__name__},
                ))

        # ── Price zero ────────────────────────────────────────────────────────
        price = payload.get("price")
        if price is not None and (price == 0 or (isinstance(price, float) and price <= 0)):
            events.append(DriftEvent(
                drift_type="price_zero",
                risk_level="critical",
                description=f"Price is zero or negative: {price!r}",
                field_name="price",
                curr_signature={"price": price},
            ))

        # ── Availability unknown ───────────────────────────────────────────────
        avail = str(payload.get("availability") or "").strip().lower()
        if "availability" in baseline and avail not in _KNOWN_AVAILABILITY:
            events.append(DriftEvent(
                drift_type="availability_unknown",
                risk_level="medium",
                description=f"Availability value not canonical: {avail!r}",
                field_name="availability",
                curr_signature={"availability": avail},
            ))

        # ── Strategy fallback ─────────────────────────────────────────────────
        strategy = str(payload.get("scraper_strategy") or "").strip().lower()
        if (
            prev_strategy in _PRIMARY_STRATEGIES
            and strategy in _FALLBACK_STRATEGIES
        ):
            events.append(DriftEvent(
                drift_type="strategy_fallback",
                risk_level="medium",
                description=(
                    f"Scraper fell back from {prev_strategy!r} to "
                    f"{strategy!r} — possible site layout change"
                ),
                field_name="scraper_strategy",
                prev_signature={"strategy": prev_strategy},
                curr_signature={"strategy": strategy},
            ))

        return events

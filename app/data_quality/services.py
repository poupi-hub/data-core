from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.data_quality.models import DataQualityRun
from app.normalization.models import (
    NormalizedCryptoSnapshot,
    NormalizedMarketCandle,
    NormalizedProduct,
    NormalizedSportsOdd,
)


class DataQualityService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def run(self, *, module: str | None = None, source_name: str | None = None, limit: int = 1000) -> dict[str, Any]:
        modules = [module] if module else list(self._rules().keys())
        results = []
        for module_name in modules:
            model, rules = self._rules()[module_name]
            query = self.db.query(model)
            if source_name:
                source_field = self._source_field(model)
                if source_field is not None:
                    query = query.filter(source_field == source_name)
            records = self._latest_records_by_raw(query.limit(limit * 3).all())[:limit]
            checked = len(records)
            passed = 0
            rule_stats = {
                rule.name: {
                    "passed": 0,
                    "failed": 0,
                    "description": rule.description,
                    "severity": rule.severity,
                }
                for rule in rules
            }
            failure_samples: list[dict[str, Any]] = []
            for record in records:
                failed_rules = []
                for rule in rules:
                    if rule.check(record):
                        rule_stats[rule.name]["passed"] += 1
                    else:
                        failed_rules.append(rule.name)
                        rule_stats[rule.name]["failed"] += 1
                if not failed_rules:
                    passed += 1
                elif len(failure_samples) < 20:
                    failure_samples.append(
                        {
                            "id": str(getattr(record, "id", "")),
                            "failed_rules": failed_rules,
                            "source": self._record_source(record),
                        }
                    )
            failed = checked - passed
            sample = records[0] if records else None
            run = DataQualityRun(
                module=module_name,
                source_name=source_name or self._record_source(sample),
                normalizer_name=getattr(sample, "normalizer_name", None),
                normalizer_version=getattr(sample, "normalizer_version", None),
                raw_schema_name=getattr(sample, "source_raw_schema_name", None),
                raw_schema_version=getattr(sample, "source_raw_schema_version", None),
                checked_count=checked,
                passed_count=passed,
                failed_count=failed,
                quality_score=passed / checked if checked else None,
                status="completed",
                metadata_json={
                    "rules": [
                        {
                            "name": rule.name,
                            "description": rule.description,
                            "severity": rule.severity,
                        }
                        for rule in rules
                    ],
                    "rule_stats": rule_stats,
                    "failure_samples": failure_samples,
                    "limit": limit,
                    "source_name_filter": source_name,
                },
            )
            self.db.add(run)
            self.db.flush()
            results.append(
                {
                    "module": module_name,
                    "source_name": source_name,
                    "checked_count": checked,
                    "passed_count": passed,
                    "failed_count": failed,
                    "quality_score": float(run.quality_score) if run.quality_score is not None else None,
                    "run_id": str(run.id),
                }
            )
        self.db.commit()
        return {"runs": results}

    @staticmethod
    def _rules() -> dict[str, tuple[type, list["QualityRule"]]]:
        return {
            "ecommerce": (
                NormalizedProduct,
                [
                    required("title"),
                    required("store_name"),
                    positive("price"),
                    allowed_values("currency", {"BRL", "USD", "EUR"}, allow_empty=True),
                    metadata_not_false("raw_success", allow_missing=True),
                ],
            ),
            "crypto": (
                NormalizedCryptoSnapshot,
                [
                    required("symbol"),
                    positive("price"),
                    non_negative("volume", allow_empty=True),
                    non_negative("market_cap", allow_empty=True),
                ],
            ),
            "trading": (
                NormalizedMarketCandle,
                [
                    required("symbol"),
                    required("timeframe"),
                    required("timestamp"),
                    positive("open"),
                    positive("high"),
                    positive("low"),
                    positive("close"),
                    non_negative("volume", allow_empty=True),
                    ohlc_consistency(),
                ],
            ),
            "sports_odds": (
                NormalizedSportsOdd,
                [
                    required("sportsbook"),
                    required("sport"),
                    required("league"),
                    required("event_external_id"),
                    required("market_type"),
                    required("selection"),
                    greater_than("odd", Decimal("1")),
                    probability_range("implied_probability", allow_empty=True),
                ],
            ),
        }

    @staticmethod
    def _source_field(model: type) -> object | None:
        for field_name in ("store_name", "source", "sportsbook"):
            if hasattr(model, field_name):
                return getattr(model, field_name)
        return None

    @staticmethod
    def _record_source(record: object | None) -> str | None:
        if record is None:
            return None
        return getattr(record, "store_name", None) or getattr(record, "source", None) or getattr(record, "sportsbook", None)

    @staticmethod
    def _latest_records_by_raw(records: list[object]) -> list[object]:
        latest: dict[object, object] = {}
        passthrough: list[object] = []
        for record in records:
            raw_id = getattr(record, "raw_collection_id", None)
            if raw_id is None:
                passthrough.append(record)
                continue
            current = latest.get(raw_id)
            if current is None or _record_sort_key(record) > _record_sort_key(current):
                latest[raw_id] = record
        return list(latest.values()) + passthrough


@dataclass(frozen=True)
class QualityRule:
    name: str
    description: str
    check: Callable[[object], bool]
    severity: str = "error"


def required(field: str) -> QualityRule:
    return QualityRule(
        name=f"{field}_required",
        description=f"{field} must be present.",
        check=lambda record: _value(record, field) not in (None, ""),
    )


def positive(field: str, *, allow_empty: bool = False) -> QualityRule:
    return greater_than(field, Decimal("0"), allow_empty=allow_empty)


def greater_than(field: str, minimum: Decimal, *, allow_empty: bool = False) -> QualityRule:
    return QualityRule(
        name=f"{field}_gt_{minimum}",
        description=f"{field} must be greater than {minimum}.",
        check=lambda record: _empty_allowed(record, field, allow_empty) or (_decimal_value(record, field) is not None and _decimal_value(record, field) > minimum),
    )


def non_negative(field: str, *, allow_empty: bool = False) -> QualityRule:
    return QualityRule(
        name=f"{field}_non_negative",
        description=f"{field} must be zero or greater.",
        check=lambda record: _empty_allowed(record, field, allow_empty) or (_decimal_value(record, field) is not None and _decimal_value(record, field) >= 0),
    )


def allowed_values(field: str, values: set[str], *, allow_empty: bool = False) -> QualityRule:
    return QualityRule(
        name=f"{field}_allowed",
        description=f"{field} must be one of: {', '.join(sorted(values))}.",
        check=lambda record: _empty_allowed(record, field, allow_empty) or str(_value(record, field)).upper() in values,
    )


def plausible_range(field: str, *, minimum: Decimal | int, maximum: Decimal | int, allow_empty: bool = False) -> QualityRule:
    min_value = Decimal(str(minimum))
    max_value = Decimal(str(maximum))
    return QualityRule(
        name=f"{field}_plausible_range",
        description=f"{field} must be between {min_value} and {max_value}.",
        check=lambda record: _empty_allowed(record, field, allow_empty)
        or (_decimal_value(record, field) is not None and min_value <= _decimal_value(record, field) <= max_value),
    )


def probability_range(field: str, *, allow_empty: bool = False) -> QualityRule:
    return plausible_range(field, minimum=0, maximum=1, allow_empty=allow_empty)


def ohlc_consistency() -> QualityRule:
    def check(record: object) -> bool:
        open_value = _decimal_value(record, "open")
        high = _decimal_value(record, "high")
        low = _decimal_value(record, "low")
        close = _decimal_value(record, "close")
        if None in (open_value, high, low, close):
            return False
        return low <= open_value <= high and low <= close <= high

    return QualityRule(
        name="ohlc_consistency",
        description="OHLC values must satisfy low <= open/close <= high.",
        check=check,
    )


def metadata_not_false(field: str, *, allow_missing: bool = False) -> QualityRule:
    return QualityRule(
        name=f"{field}_not_false",
        description=f"normalization metadata field {field} must not be false.",
        check=lambda record: _metadata_value(record, field) is not False
        and (allow_missing or _metadata_value(record, field) is not None),
    )


def _empty_allowed(record: object, field: str, allow_empty: bool) -> bool:
    return allow_empty and _value(record, field) in (None, "")


def _value(record: object, field: str) -> Any:
    return getattr(record, field, None)


def _metadata_value(record: object, field: str) -> Any:
    metadata = getattr(record, "normalization_metadata_json", None)
    if not isinstance(metadata, dict):
        return None
    return metadata.get(field)


def _decimal_value(record: object, field: str) -> Decimal | None:
    value = _value(record, field)
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _record_sort_key(record: object) -> tuple[str, str]:
    normalized_at = getattr(record, "normalized_at", None)
    collected_at = getattr(record, "collected_at", None)
    timestamp = normalized_at or collected_at or ""
    return (str(timestamp), str(getattr(record, "id", "")))

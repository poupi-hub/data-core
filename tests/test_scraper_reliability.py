"""Tests for Phase RELIABILITY — scraper drift, quality, and anti-bot components.

All tests are pure unit tests (no DB required).
"""

from __future__ import annotations

import pytest

from app.scrapers.anti_bot import AntiBotDetector, AntiBotResult
from app.scrapers.diagnostics import DiagnosticsEngine
from app.scrapers.drift import DriftEvent, StructuralDriftDetector
from app.scrapers.quality import PayloadQualityScorer, QualityResult


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _good_product(**overrides) -> dict:
    base = {
        "title": "Fralda Pampers Confort Sec G",
        "price": 99.90,
        "availability": "in_stock",
        "source_id": "1351898",
        "store_name": "drogasil",
        "scraper_strategy": "vtex_api",
        "brand": "Pampers",
        "url": "https://www.drogasil.com.br/fralda-1351898.html",
        "currency": "BRL",
    }
    base.update(overrides)
    return base


# ──────────────────────────────────────────────────────────────────────────────
# PayloadQualityScorer
# ──────────────────────────────────────────────────────────────────────────────

class TestPayloadQualityScorer:
    def setup_method(self):
        self.scorer = PayloadQualityScorer()

    def test_perfect_vtex_api_product(self):
        result = self.scorer.score(_good_product(), latency_seconds=0.3)
        # title(20) + price(25) + avail(15) + source_id(15) + vtex_api(10) + latency~15
        assert result.score >= 90
        assert result.grade == "excellent"
        assert result.is_acceptable

    def test_missing_title_penalised(self):
        result = self.scorer.score(_good_product(title=""), latency_seconds=0.3)
        assert result.breakdown["title"] == 0
        assert "title invalid" in result.issues[0]

    def test_zero_price_penalised(self):
        result = self.scorer.score(_good_product(price=0), latency_seconds=0.3)
        assert result.breakdown["price"] == 0
        assert any("price" in issue for issue in result.issues)

    def test_none_price_penalised(self):
        result = self.scorer.score(_good_product(price=None), latency_seconds=0.3)
        assert result.breakdown["price"] == 0

    def test_implausible_price_penalised(self):
        result = self.scorer.score(_good_product(price=999_999.0), latency_seconds=0.3)
        assert result.breakdown["price"] == 0

    def test_unknown_availability_penalised(self):
        result = self.scorer.score(_good_product(availability="maybe"), latency_seconds=0.3)
        assert result.breakdown["availability"] == 0
        assert any("availability" in i for i in result.issues)

    def test_missing_source_id_penalised(self):
        result = self.scorer.score(_good_product(source_id=""), latency_seconds=0.3)
        assert result.breakdown["source_id"] == 0

    def test_strategy_json_ld_lower_bonus_than_vtex(self):
        vtex = self.scorer.score(_good_product(scraper_strategy="vtex_api"), latency_seconds=1.0)
        jsonld = self.scorer.score(_good_product(scraper_strategy="json_ld"), latency_seconds=1.0)
        assert vtex.score > jsonld.score

    def test_high_latency_reduces_score(self):
        fast = self.scorer.score(_good_product(), latency_seconds=0.3)
        slow = self.scorer.score(_good_product(), latency_seconds=15.0)
        assert fast.score > slow.score
        assert slow.breakdown["latency"] == 0

    def test_no_latency_neutral(self):
        result = self.scorer.score(_good_product(), latency_seconds=None)
        assert result.breakdown["latency"] == 0

    def test_grade_thresholds(self):
        assert self.scorer.score(_good_product(price=None, title="", source_id=""), latency_seconds=None).grade in (
            "poor", "critical", "fair"
        )

    def test_score_clamped_to_100(self):
        result = self.scorer.score(_good_product(), latency_seconds=0.1)
        assert result.score <= 100


# ──────────────────────────────────────────────────────────────────────────────
# AntiBotDetector
# ──────────────────────────────────────────────────────────────────────────────

class TestAntiBotDetector:
    def setup_method(self):
        self.detector = AntiBotDetector()

    def test_clean_response_not_detected(self):
        result = self.detector.from_raw(200, "<html><body>" + "x" * 5000 + "</body></html>")
        assert not result.detected
        assert result.detection_type == "none"

    def test_429_is_rate_limit(self):
        result = self.detector.from_raw(429, "")
        assert result.detected
        assert result.detection_type == "rate_limit"
        assert result.confidence >= 90

    def test_403_is_access_denied(self):
        result = self.detector.from_raw(403, "some normal page content here")
        assert result.detected
        assert result.detection_type == "access_denied"

    def test_403_with_captcha_body(self):
        result = self.detector.from_raw(403, "please complete a recaptcha to continue")
        assert result.detected
        assert result.detection_type == "captcha"

    def test_cloudflare_just_a_moment(self):
        body = "<html><head><title>Just a moment...</title></head><body>checking your browser</body></html>"
        body += "x" * 5000  # pad to avoid honeypot
        result = self.detector.from_raw(200, body)
        assert result.detected
        assert result.detection_type == "cloudflare"

    def test_recaptcha_in_200_body(self):
        body = "<html><body>Please verify: <div class='recaptcha'></div></body></html>" + "x" * 3000
        result = self.detector.from_raw(200, body)
        assert result.detected
        assert result.detection_type == "captcha"

    def test_rate_limit_in_200_body(self):
        body = "<html><body>rate limit exceeded — try again later</body></html>" + "x" * 3000
        result = self.detector.from_raw(200, body)
        assert result.detected
        assert result.detection_type == "rate_limit"

    def test_honeypot_tiny_200(self):
        result = self.detector.from_raw(200, "<html><body>hi</body></html>")
        assert result.detected
        assert result.detection_type == "honeypot"

    def test_redirect_to_captcha_page(self):
        result = self.detector.from_raw(200, "x" * 5000, redirect_url="https://example.com/captcha?ref=1")
        assert result.detected
        assert result.detection_type == "redirect_loop"

    def test_from_response_uses_status_and_text(self):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 429
        resp.text = ""
        resp.url = "https://example.com/"
        result = self.detector.from_response(resp)
        assert result.detected
        assert result.detection_type == "rate_limit"


# ──────────────────────────────────────────────────────────────────────────────
# StructuralDriftDetector
# ──────────────────────────────────────────────────────────────────────────────

class TestStructuralDriftDetector:
    def setup_method(self):
        self.detector = StructuralDriftDetector()

    def _baseline(self, n: int = 5) -> dict[str, str]:
        """Build a baseline from N identical good payloads."""
        return self.detector.build_baseline([_good_product() for _ in range(n)])

    def test_no_drift_for_identical_payload(self):
        baseline = self._baseline()
        events = self.detector.detect(_good_product(), baseline)
        # Only "field_added" drift for fields not in baseline expected to be empty
        # (all fields in good_product should be in majority baseline)
        non_added = [e for e in events if e.drift_type != "field_added"]
        assert non_added == []

    def test_missing_price_is_critical(self):
        baseline = self._baseline()
        payload = _good_product()
        del payload["price"]
        events = self.detector.detect(payload, baseline)
        price_event = next(e for e in events if e.field_name == "price")
        assert price_event.drift_type == "field_missing"
        assert price_event.risk_level == "critical"

    def test_missing_title_is_high_risk(self):
        baseline = self._baseline()
        payload = _good_product()
        del payload["title"]
        events = self.detector.detect(payload, baseline)
        title_event = next(e for e in events if e.field_name == "title")
        assert title_event.risk_level == "high"

    def test_price_type_changed_is_critical(self):
        baseline = self._baseline()  # price → float
        payload = _good_product(price="99.90")  # now str
        events = self.detector.detect(payload, baseline)
        price_event = next((e for e in events if e.field_name == "price" and e.drift_type == "type_changed"), None)
        assert price_event is not None
        assert price_event.risk_level == "critical"

    def test_price_zero_triggers_drift(self):
        baseline = self._baseline()
        payload = _good_product(price=0.0)
        events = self.detector.detect(payload, baseline)
        zero_event = next(e for e in events if e.drift_type == "price_zero")
        assert zero_event.risk_level == "critical"

    def test_new_field_is_low_risk(self):
        baseline = self._baseline()
        payload = _good_product()
        payload["new_field"] = "surprise"
        events = self.detector.detect(payload, baseline)
        added = next((e for e in events if e.drift_type == "field_added" and e.field_name == "new_field"), None)
        assert added is not None
        assert added.risk_level == "low"

    def test_availability_unknown_is_medium(self):
        baseline = self._baseline()
        payload = _good_product(availability="maybe_in_stock")
        events = self.detector.detect(payload, baseline)
        avail_event = next((e for e in events if e.drift_type == "availability_unknown"), None)
        assert avail_event is not None
        assert avail_event.risk_level == "medium"

    def test_strategy_fallback_detected(self):
        baseline = self._baseline()
        payload = _good_product(scraper_strategy="meta_css")
        events = self.detector.detect(payload, baseline, prev_strategy="vtex_api")
        fallback_event = next((e for e in events if e.drift_type == "strategy_fallback"), None)
        assert fallback_event is not None
        assert fallback_event.risk_level == "medium"

    def test_empty_baseline_returns_no_events(self):
        events = self.detector.detect(_good_product(), {})
        assert events == []

    def test_build_baseline_majority_vote(self):
        payloads = [_good_product() for _ in range(4)]
        # Add 1 payload missing "brand" — should still be in baseline (4/5 have it)
        p = _good_product()
        del p["brand"]
        payloads.append(p)
        baseline = self.detector.build_baseline(payloads)
        assert "brand" in baseline  # 4/5 = 80% ≥ 50%

    def test_build_baseline_excludes_rare_fields(self):
        payloads = [_good_product() for _ in range(8)]
        # Only 3 out of 8+1 = 9 total have "discount_badge"
        for i in range(3):
            payloads[i]["discount_badge"] = "20% OFF"
        baseline = self.detector.build_baseline(payloads)
        # 3/8 = 37.5% < 50% threshold — excluded
        assert "discount_badge" not in baseline


# ──────────────────────────────────────────────────────────────────────────────
# DiagnosticsEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestDiagnosticsEngine:
    def setup_method(self):
        self.engine = DiagnosticsEngine()

    def test_no_issues_when_everything_ok(self):
        results = self.engine.evaluate(
            source_name="drogasil",
            drift_events=[],
            fallback_count=1,
            total_count=10,
            anti_bot_count=0,
            window_hours=1,
            avg_quality_score=85,
            scraper_enabled=True,
        )
        assert results == []

    def test_disabled_scraper_is_error(self):
        results = self.engine.evaluate("drogasil", scraper_enabled=False)
        assert len(results) == 1
        assert results[0].code == "scraper_disabled"
        assert results[0].severity == "error"

    def test_critical_drift_detected(self):
        results = self.engine.evaluate(
            "drogasil",
            drift_events=[{"drift_type": "price_zero", "risk_level": "critical"}],
        )
        drift_result = next(r for r in results if r.code == "drift_detected")
        assert drift_result.severity == "critical"

    def test_high_drift_detected(self):
        results = self.engine.evaluate(
            "drogasil",
            drift_events=[{"drift_type": "field_missing", "risk_level": "high"}],
        )
        drift_result = next(r for r in results if r.code == "drift_detected")
        assert drift_result.severity in ("error", "critical")

    def test_low_drift_not_flagged(self):
        results = self.engine.evaluate(
            "drogasil",
            drift_events=[{"drift_type": "field_added", "risk_level": "low"}],
        )
        codes = [r.code for r in results]
        assert "drift_detected" not in codes

    def test_fallback_rate_above_threshold(self):
        results = self.engine.evaluate(
            "drogasil",
            fallback_count=5,
            total_count=10,  # 50% > 40% threshold
            window_hours=1,
        )
        fallback_result = next(r for r in results if r.code == "fallback_excessive")
        assert fallback_result.severity == "warning"
        assert fallback_result.context["fallback_rate"] == 0.5

    def test_fallback_rate_below_threshold(self):
        results = self.engine.evaluate(
            "drogasil",
            fallback_count=3,
            total_count=10,  # 30% < 40% threshold
            window_hours=1,
        )
        codes = [r.code for r in results]
        assert "fallback_excessive" not in codes

    def test_anti_bot_growing(self):
        results = self.engine.evaluate(
            "drogasil",
            anti_bot_count=5,
            window_hours=1.0,  # 5/h > threshold=2
        )
        ab_result = next(r for r in results if r.code == "anti_bot_growing")
        assert ab_result.severity == "warning"

    def test_low_quality_score(self):
        results = self.engine.evaluate(
            "drogasil",
            avg_quality_score=35,  # < threshold=50
        )
        q_result = next(r for r in results if r.code == "payload_quality_low")
        assert q_result.severity == "warning"

    def test_multiple_issues_reported(self):
        results = self.engine.evaluate(
            "drogasil",
            drift_events=[{"drift_type": "price_zero", "risk_level": "critical"}],
            fallback_count=5,
            total_count=10,
            anti_bot_count=10,
            window_hours=1,
            avg_quality_score=20,
            scraper_enabled=False,
        )
        codes = {r.code for r in results}
        assert "scraper_disabled" in codes
        assert "drift_detected" in codes
        assert "fallback_excessive" in codes
        assert "anti_bot_growing" in codes
        assert "payload_quality_low" in codes

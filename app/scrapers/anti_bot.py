"""AntiBotDetector — detect bot-blocking patterns in HTTP responses.

Detects the following categories:
  - captcha         — CAPTCHA / challenge page in response body
  - cloudflare      — Cloudflare JS challenge or block page
  - rate_limit      — HTTP 429 or body text matching rate-limit patterns
  - access_denied   — HTTP 403 with typical bot-block body phrases
  - honeypot        — suspiciously small/empty response with 200 status
  - redirect_loop   — unexpected redirect to login / captcha page
  - none            — no detection

Each detection event includes a confidence (0-100) and a short reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# ── Body pattern banks ────────────────────────────────────────────────────────

_CAPTCHA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"recaptcha", re.I),
    re.compile(r"hcaptcha", re.I),
    re.compile(r"cf-challenge", re.I),
    re.compile(r"challenge-form", re.I),
    re.compile(r"prove you are (not a robot|human)", re.I),
    re.compile(r"i'm not a robot", re.I),
    re.compile(r"verificação de segurança", re.I),
]

_CLOUDFLARE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"cloudflare", re.I),
    re.compile(r"cf-ray", re.I),
    re.compile(r"just a moment", re.I),
    re.compile(r"checking your browser", re.I),
    re.compile(r"ddos protection by cloudflare", re.I),
    re.compile(r"<title>attention required", re.I),
]

_RATE_LIMIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rate.?limit", re.I),
    re.compile(r"too many requests", re.I),
    re.compile(r"muitas solicitações", re.I),
    re.compile(r"you have been blocked", re.I),
    re.compile(r"request limit exceeded", re.I),
]

_ACCESS_DENIED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"access denied", re.I),
    re.compile(r"acesso negado", re.I),
    re.compile(r"403 forbidden", re.I),
    re.compile(r"bot detection", re.I),
    re.compile(r"automated (requests|access)", re.I),
    re.compile(r"scraping (is )?not allowed", re.I),
]

# Redirect URLs that indicate blocking
_BLOCK_REDIRECT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/blocked", re.I),
    re.compile(r"/captcha", re.I),
    re.compile(r"/access-denied", re.I),
    re.compile(r"challenge", re.I),
]

# Minimum bytes for a "real" product page
_MIN_PRODUCT_PAGE_BYTES = 2_000


@dataclass
class AntiBotResult:
    detection_type: str  # "none" | "captcha" | "cloudflare" | "rate_limit" | "access_denied" | "honeypot" | "redirect_loop"
    confidence: int  # 0-100
    reason: str
    http_status: int | None = None

    @property
    def detected(self) -> bool:
        return self.detection_type != "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "detection_type": self.detection_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "http_status": self.http_status,
        }


class AntiBotDetector:
    """Analyse HTTP response objects (or raw body/status) for bot-blocking signals.

    Usage::

        detector = AntiBotDetector()

        # From httpx-style response
        result = detector.from_response(resp, url=url)

        # From raw status + body text
        result = detector.from_raw(status_code=403, body="Access Denied", url=url)

        if result.detected:
            print(result.detection_type, result.confidence)
    """

    def from_raw(
        self,
        status_code: int,
        body: str,
        url: str = "",
        redirect_url: str | None = None,
    ) -> AntiBotResult:
        """Detect from raw HTTP status + body string."""
        # ── HTTP 429 → rate limit ─────────────────────────────────────────────
        if status_code == 429:
            return AntiBotResult("rate_limit", 95, "HTTP 429 Too Many Requests", status_code)

        # ── HTTP 403 → check body for context ────────────────────────────────
        if status_code == 403:
            # CAPTCHA on 403
            for pat in _CAPTCHA_PATTERNS:
                if pat.search(body):
                    return AntiBotResult("captcha", 90, f"403 + body matches {pat.pattern!r}", status_code)
            # Cloudflare on 403
            for pat in _CLOUDFLARE_PATTERNS:
                if pat.search(body):
                    return AntiBotResult("cloudflare", 92, f"403 + body matches {pat.pattern!r}", status_code)
            # Generic access denied
            return AntiBotResult("access_denied", 80, "HTTP 403 Forbidden", status_code)

        # ── Redirect to block page ────────────────────────────────────────────
        if redirect_url:
            for pat in _BLOCK_REDIRECT_PATTERNS:
                if pat.search(redirect_url):
                    return AntiBotResult(
                        "redirect_loop", 85, f"Redirect to block URL: {redirect_url}", status_code
                    )

        # ── 200 with suspicious body ──────────────────────────────────────────
        if status_code == 200:
            # Cloudflare JS challenge served as 200 on some CDN configs
            for pat in _CLOUDFLARE_PATTERNS:
                if pat.search(body):
                    return AntiBotResult("cloudflare", 88, f"200 + body matches {pat.pattern!r}", status_code)

            # CAPTCHA page
            for pat in _CAPTCHA_PATTERNS:
                if pat.search(body):
                    return AntiBotResult("captcha", 85, f"200 + body matches {pat.pattern!r}", status_code)

            # Rate limit message in body
            for pat in _RATE_LIMIT_PATTERNS:
                if pat.search(body):
                    return AntiBotResult("rate_limit", 80, f"200 + body matches {pat.pattern!r}", status_code)

            # Honeypot: response too small to be real product page
            if len(body.encode("utf-8", errors="replace")) < _MIN_PRODUCT_PAGE_BYTES:
                return AntiBotResult(
                    "honeypot",
                    60,
                    f"200 response body too small ({len(body)} chars < {_MIN_PRODUCT_PAGE_BYTES} bytes)",
                    status_code,
                )

        return AntiBotResult("none", 0, "no blocking signals detected", status_code)

    def from_response(self, response: Any, url: str = "") -> AntiBotResult:
        """Detect from an httpx-style response object."""
        status = getattr(response, "status_code", 200)
        body = getattr(response, "text", "") or ""
        # httpx stores the final URL in response.url after redirects
        redirect_url = str(getattr(response, "url", "") or "")
        return self.from_raw(status_code=status, body=body, url=url, redirect_url=redirect_url)

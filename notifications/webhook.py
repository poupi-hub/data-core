import logging
from typing import Any

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


def send_webhook(payload: dict[str, Any]) -> bool:
    """POST payload to settings.alert_webhook_url. Returns True on success."""
    url = settings.alert_webhook_url
    if not url:
        return False
    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Alert webhook delivered", extra={"status": response.status_code})
        return True
    except Exception as exc:
        logger.warning("Alert webhook failed", extra={"error": str(exc), "url": url})
        return False

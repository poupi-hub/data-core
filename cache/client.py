import json
import logging
from typing import Any

import redis

from core.config import settings

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis | None:
    global _client
    if not settings.cache_enabled:
        return None
    if _client is None:
        try:
            _client = redis.from_url(settings.redis_url, decode_responses=True)
            _client.ping()
        except Exception as exc:
            logger.warning("Redis unavailable — caching disabled: %s", exc)
            _client = None
    return _client


def cache_get(key: str) -> Any | None:
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception as exc:
        logger.debug("Cache GET error for %s: %s", key, exc)
        return None


def cache_set(key: str, value: Any, ttl_seconds: int = 60) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps(value, default=str))
    except Exception as exc:
        logger.debug("Cache SET error for %s: %s", key, exc)


def cache_invalidate(pattern: str) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        keys = client.keys(pattern)
        if keys:
            client.delete(*keys)
    except Exception as exc:
        logger.debug("Cache invalidate error for %s: %s", pattern, exc)

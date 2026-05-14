import hashlib

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from core.config import settings


def rate_limit_key_func(request: Request) -> str:
    """Key rate limits by API key in authenticated deployments, otherwise by IP."""
    if settings.api_key_enabled:
        api_key = request.headers.get("X-API-Key")
        if api_key:
            digest = hashlib.sha256(api_key.encode()).hexdigest()
            return f"api-key:{digest}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=rate_limit_key_func)

from collections.abc import Mapping
from typing import Any

from svix import Webhook

from app.config import get_settings


def verify_clerk_webhook(payload: str | bytes, headers: Mapping[str, str]) -> dict[str, Any]:
    secret = get_settings().clerk_webhook_signing_secret
    if not secret:
        raise ValueError("Clerk webhook signing secret is not configured")
    verified = Webhook(secret).verify(payload, dict(headers))
    if not isinstance(verified, dict):
        raise TypeError("Clerk webhook payload must be an object")
    return verified

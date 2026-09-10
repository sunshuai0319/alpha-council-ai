from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from svix import Webhook

from app.config import get_settings
from app.db.models import ClerkWebhookEvent, User


def verify_clerk_webhook(payload: str | bytes, headers: Mapping[str, str]) -> dict[str, Any]:
    secret = get_settings().clerk_webhook_signing_secret
    if not secret:
        raise ValueError("Clerk webhook signing secret is not configured")
    verified = Webhook(secret).verify(payload, dict(headers))
    if not isinstance(verified, dict):
        raise TypeError("Clerk webhook payload must be an object")
    return verified


def process_clerk_event(db: Session, event: dict[str, Any]) -> bool:
    event_id = event.get("id")
    event_type = event.get("type")
    data = event.get("data")
    if not isinstance(event_id, str) or not isinstance(event_type, str) or not isinstance(data, dict):
        raise TypeError("invalid Clerk event")
    if db.scalar(select(ClerkWebhookEvent).where(ClerkWebhookEvent.id == event_id)) is not None:
        return False
    clerk_user_id = data.get("id")
    if not isinstance(clerk_user_id, str):
        raise TypeError("Clerk event has no user id")
    user = db.scalar(select(User).where(User.clerk_user_id == clerk_user_id))
    if user is None:
        user = User(id=str(uuid4()), clerk_user_id=clerk_user_id)
        db.add(user)
    emails = data.get("email_addresses") or []
    primary_id = data.get("primary_email_address_id")
    primary_email = next(
        (
            item.get("email_address")
            for item in emails
            if isinstance(item, dict) and item.get("id") == primary_id
        ),
        None,
    )
    user.email = primary_email or user.email
    names = [str(data.get("first_name") or "").strip(), str(data.get("last_name") or "").strip()]
    user.display_name = " ".join(part for part in names if part) or data.get("username") or user.display_name
    if event_type == "user.deleted":
        user.status = "deleted"
    db.add(ClerkWebhookEvent(id=event_id, event_type=event_type, payload=event))
    db.commit()
    return True

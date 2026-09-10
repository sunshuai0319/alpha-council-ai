from fastapi import APIRouter, Request

from app.auth.webhooks import process_clerk_event, verify_clerk_webhook
from app.db.session import SessionLocal

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/webhook")
async def clerk_webhook(request: Request) -> dict[str, bool]:
    payload = await request.body()
    event = verify_clerk_webhook(payload, request.headers)
    with SessionLocal() as db:
        return {"processed": process_clerk_event(db, event)}

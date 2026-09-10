from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.clerk import AuthPrincipal, extract_bearer_token, verify_session_token
from app.db.session import get_db


def get_current_principal(request: Request) -> AuthPrincipal:
    token = extract_bearer_token(request.headers.get("authorization"))
    return verify_session_token(token)


def get_current_user(
    principal: AuthPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    from app.auth.clerk import sync_local_user

    return sync_local_user(db, principal)

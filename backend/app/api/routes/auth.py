from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me")
def me(user=Depends(get_current_user)) -> dict[str, str | None]:
    return {"id": user.id, "clerk_user_id": user.clerk_user_id, "email": user.email}

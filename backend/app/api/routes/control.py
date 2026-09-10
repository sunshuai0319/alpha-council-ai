from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api/control", tags=["control"])


@router.post("/pause")
def pause(
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.pause(user.id)


@router.post("/resume")
def resume(
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.resume(user.id)

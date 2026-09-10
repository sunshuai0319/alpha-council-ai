from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/market")
def market(
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.market(user.id)


@router.get("/decisions")
def decisions(
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.decisions(user.id)


@router.get("/portfolio")
def portfolio(
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.portfolio(user.id)


@router.get("/events")
def events(
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.events(user.id)

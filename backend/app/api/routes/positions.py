from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.post("/{symbol}/close")
def close_position(
    symbol: str,
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    return service.close_position(user.id, symbol.upper())

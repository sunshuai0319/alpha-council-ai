from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api", tags=["dashboard"])


class LocaleUpdate(BaseModel):
    locale: Literal["zh-CN", "en-US"]


@router.put("/preferences/locale")
def set_locale(
    payload: LocaleUpdate,
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, str]:
    """记住界面语言，worker 会按它决定 LLM 分析文本的语言。"""

    return {"locale": service.set_locale(user.id, payload.locale)}


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

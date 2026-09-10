from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from app.agents.graph import build_trading_cycle_graph
from app.api.dependencies import get_cycle_service
from app.auth.dependencies import get_current_user
from app.config import get_settings
from app.exchange.fixtures import FixtureExchangeClient
from app.services.cycle import TradingCycleService

router = APIRouter(prefix="/api/test", tags=["test-support"])


@router.post("/run-cycle")
def run_fixture_cycle(
    x_test_exchange: str | None = Header(default=None),
    user=Depends(get_current_user),
    service: TradingCycleService = Depends(get_cycle_service),
) -> dict[str, Any]:
    settings = get_settings()
    if settings.app_env != "test":
        raise HTTPException(status_code=404, detail="test support is disabled")
    if x_test_exchange != "fixture":
        raise HTTPException(status_code=400, detail="set X-Test-Exchange: fixture")
    fixture_service = TradingCycleService(
        db=service.db,
        settings=settings,
        exchange_factory=FixtureExchangeClient,
        graph_factory=lambda: build_trading_cycle_graph(settings=settings),
    )
    return fixture_service.run(user.id).as_dict()

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.schemas import Candle, MarketSnapshot
from app.exchange.base import ExchangeBalance
from app.services.cycle import TradingCycleService


class FakeExchange:
    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        del limit
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=1_700_000_000_000,
                open=100,
                high=101,
                low=99,
                close=100,
                volume=10,
            )
        ]

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            captured_at=int(datetime.now(UTC).timestamp() * 1000),
            last_price=100,
        )

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", Decimal(10000), Decimal(10000), Decimal(0), Decimal(0))]

    def get_positions(self) -> list[object]:
        return []

    def place_order(self, request: object) -> object:
        raise AssertionError("invalid committee output must not place an order")


class FailingLLM:
    def complete_json(self, prompt: str) -> str:
        del prompt
        raise TimeoutError("LLM unavailable")


def test_cycle_failure_persists_hold_decision() -> None:
    service = TradingCycleService(exchange_factory=FakeExchange)
    result = service.run(user_id="u-1", llm=FailingLLM())
    assert result.action == "HOLD"
    assert result.persisted is True

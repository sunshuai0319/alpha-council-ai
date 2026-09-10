from datetime import UTC, datetime
from decimal import Decimal

from app.domain.enums import Action, RiskStatus
from app.domain.schemas import (
    Candle,
    MarketSnapshot,
    RiskDecision,
    TradeProposal,
    TradingCycleState,
)
from app.exchange.base import ExchangeBalance, ExchangeOrder, OrderRequest
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


class RecordingExchange:
    """记录下单请求并回报 FILLED。"""

    def __init__(self, balance: Decimal = Decimal(10000)) -> None:
        self.requests: list[OrderRequest] = []
        self._balance = balance

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", self._balance, self._balance, Decimal(0), Decimal(0))]

    def get_positions(self) -> list[object]:
        return []

    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        self.requests.append(request)
        return ExchangeOrder(
            order_id="order-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            position_side=request.position_side,
            status="FILLED",
            order_type=request.order_type,
            quantity=request.quantity,
            executed_quantity=request.quantity,
            price=Decimal(100),
            average_price=Decimal(100),
            time_in_force=None,
            created_at=None,
            updated_at=None,
        )


def _long_state(*, price: float = 100, pct: float = 0.1) -> TradingCycleState:
    return TradingCycleState(
        user_id="u-1",
        cycle_id="cycle-1",
        started_at=1,
        symbol="BTC-USDT",
        market_snapshot=MarketSnapshot(
            symbol="BTC-USDT", captured_at=int(datetime.now(UTC).timestamp() * 1000), last_price=price
        ),
        trade_proposal=TradeProposal(
            proposal_id="proposal-1",
            action=Action.LONG,
            symbol="BTC-USDT",
            side="BUY",
            position_size_pct=pct,
            leverage=1,
            stop_loss=price * 0.9,
            valid_until=9_999_999_999_999,
            confidence=0.8,
            reasoning_summary="test",
            evidence_refs=["evidence-1"],
            model_version="test",
            trace_id="trace-1",
        ),
    )


def test_entry_quantity_is_notional_over_price() -> None:
    """数量 = 名义价值 / 价格，名义价值 = 余额 × position_size_pct。

    只除以价格会丢掉余额因子：余额 10000 时下单量会小一万倍，远低于交易所
    最小下单量而被拒。
    """
    exchange = RecordingExchange(balance=Decimal(10000))
    service = TradingCycleService(exchange_factory=lambda: exchange)

    service._execute(exchange, _long_state(price=100, pct=0.1), RiskDecision(status=RiskStatus.ALLOWED))

    # 10000 × 0.1 = 1000 名义价值；1000 / 100 = 10
    assert exchange.requests[0].quantity == Decimal(10)

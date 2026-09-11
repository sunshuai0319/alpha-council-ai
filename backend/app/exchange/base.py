from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.schemas import Candle, MarketMicrostructure, MarketSnapshot


class ExchangeError(RuntimeError):
    """A normalized error raised by an exchange adapter."""


@dataclass(frozen=True)
class ContractInfo:
    symbol: str
    price_precision: int
    quantity_precision: int
    contract_value: Decimal
    min_leverage: int
    max_leverage: int
    min_quantity: Decimal
    max_quantity: Decimal | None = None


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    position_side: str
    order_type: str
    quantity: Decimal
    client_order_id: str
    price: Decimal | None = None
    time_in_force: str | None = None
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None
    reduce_only: bool = False


@dataclass(frozen=True)
class ExchangeOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    position_side: str
    status: str
    order_type: str
    quantity: Decimal
    executed_quantity: Decimal
    price: Decimal
    average_price: Decimal
    time_in_force: str | None
    created_at: datetime | None
    updated_at: datetime | None
    reduce_only: bool = False
    raw: dict | None = None


@dataclass(frozen=True)
class ExchangeBalance:
    asset: str
    balance: Decimal
    available_balance: Decimal
    frozen: Decimal
    unrealized_pnl: Decimal
    raw: dict | None = None


@dataclass(frozen=True)
class ExchangePosition:
    position_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_value: Decimal
    margin: Decimal
    leverage: int
    unrealized_pnl: Decimal
    liquidation_price: Decimal | None
    raw: dict | None = None


@dataclass(frozen=True)
class ExchangeFill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    position_side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    filled_at: datetime
    raw: dict | None = None


class ExchangeClient(Protocol):
    @property
    def supports_trade_fills(self) -> bool:
        """False 表示该账户没有成交流水接口。

        对账层据此决定能否用 fill 推算已实现盈亏，而不是靠捕获异常做能力探测。
        """

        ...

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]: ...

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot: ...

    def get_microstructure(self, symbol: str) -> MarketMicrostructure: ...

    def get_contracts(self) -> list[ContractInfo]: ...

    def get_balances(self) -> list[ExchangeBalance]: ...

    def get_positions(self) -> list[ExchangePosition]: ...

    def place_order(self, request: OrderRequest) -> ExchangeOrder: ...

    def get_order(self, order_id: str) -> ExchangeOrder: ...

    def get_order_by_client_id(self, client_order_id: str) -> ExchangeOrder | None: ...

    def get_trades(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[ExchangeFill]: ...

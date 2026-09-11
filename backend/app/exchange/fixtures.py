from datetime import UTC, datetime
from decimal import Decimal
from time import time

from app.domain.schemas import Candle, MarketMicrostructure, MarketSnapshot
from app.exchange.base import (
    ExchangeBalance,
    ExchangeFill,
    ExchangeOrder,
    ExchangePosition,
    OrderRequest,
)

CONTRACT_INFO_RESPONSE = {
    "assets": [{"asset": "USDT", "marginAvailable": True}],
    "rateLimits": [],
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "displaySymbol": "BTCUSDT",
            "pricePrecision": 1,
            "quantityPrecision": 6,
            "contractVal": 0.000001,
            "minLeverage": 1,
            "maxLeverage": 408,
            "minOrderSize": 0.0001,
            "maxOrderSize": 10000,
        }
    ],
}

KLINES_RESPONSE = [[1700000000000, "1", "2", "0.5", "1.5", "10", 1700000299999, "15", 3, "5", "7.5"]]

#: 字段对齐实测的真实响应（见 docs/weex-virtual-api.md §1.2），
#: 别只保留用到的那几个 —— fixture 失真会让采集层的缺失一直测不出来。
TICKER_RESPONSE = {
    "symbol": "BTCUSDT",
    "lastPrice": "1.5",
    "bidPrice": "1.4",
    "askPrice": "1.6",
    "volume": "100",
    "quoteVolume": "150",
    "openPrice": "1.4",
    "highPrice": "1.7",
    "lowPrice": "1.3",
    "priceChangePercent": "0.0714",
    "markPrice": "1.5",
    "indexPrice": "1.51",
    "closeTime": 1700000299999,
}

#: 微观结构四个端点的响应，字段对齐实测（见 docs/weex-virtual-api.md §1）。
DEPTH_RESPONSE = {
    "asks": [["77230.0", "1.0"], ["77230.2", "1.0"]],
    "bids": [["77229.9", "3.0"], ["77229.7", "1.0"]],
}

TRADES_RESPONSE = [
    {"price": "77229.9", "qty": "1.0", "isBuyerMaker": False},
    {"price": "77229.9", "qty": "1.0", "isBuyerMaker": True},
    {"price": "77229.9", "qty": "2.0", "isBuyerMaker": False},
]

FUNDING_RATE_RESPONSE = [{"fundingRate": "0.00003006", "fundingTime": 1789084800000}]

OPEN_INTEREST_RESPONSE = {"openInterest": "140652.2160", "time": 1789109251138}

DEMO_BALANCE_RESPONSE = [
    {
        "asset": "SUSDT",
        "balance": "10000",
        "availableBalance": "9950",
        "frozen": "50",
        "unrealizePnl": "10",
    }
]

DEMO_POSITION_RESPONSE = [
    {
        "id": 123,
        "asset": "SUSDT",
        "symbol": "BTCSUSDT",
        "side": "LONG",
        "leverage": "3",
        "size": "0.01",
        "openValue": "15",
        "marginSize": "5",
        "unrealizePnl": "0.5",
        "liquidatePrice": "0",
    }
]

ORDER_ACCEPTED_RESPONSE = {
    "orderId": "702345678901234567",
    "clientOrderId": "alpha-0001",
    "success": True,
    "errorCode": "",
    "errorMessage": "",
}

ORDER_INFO_RESPONSE = {
    "orderId": 702345678901234567,
    "symbol": "BTCSUSDT",
    "side": "BUY",
    "positionSide": "LONG",
    "status": "FILLED",
    "type": "MARKET",
    "time": 1700000000000,
    "price": "1.5",
    "origQty": "0.01",
    "executedQty": "0.01",
    "avgPrice": "1.5",
    "updateTime": 1700000001000,
    "clientOrderId": "alpha-0001",
    "timeInForce": "GTC",
    "reduceOnly": False,
}

TRADE_RESPONSE = [
    {
        "id": 801234567890123456,
        "orderId": 702345678901234567,
        "symbol": "BTCSUSDT",
        "buyer": True,
        "commission": "0.001",
        "commissionAsset": "SUSDT",
        "maker": False,
        "price": "1.5",
        "qty": "0.01",
        "quoteQty": "0.015",
        "realizedPnl": "0",
        "side": "BUY",
        "positionSide": "LONG",
        "time": 1700000001000,
    }
]


class FixtureExchangeClient:
    """Deterministic WEEX-like client for local API and browser smoke tests."""

    supports_trade_fills = True

    def __init__(self) -> None:
        self._orders: dict[str, ExchangeOrder] = {}

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        del limit
        now_ms = int(time() * 1000)
        price = 100_000.0 if symbol.upper().startswith("BTC") else 4_000.0
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=now_ms - 300_000,
                open=price - 25,
                high=price + 45,
                low=price - 65,
                close=price,
                volume=123.4,
                source="fixture",
            )
        ]

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        price = 100_000.0 if symbol.upper().startswith("BTC") else 4_000.0
        return MarketSnapshot(
            symbol=symbol,
            captured_at=int(time() * 1000),
            last_price=price,
            bid=price - 1,
            ask=price + 1,
            funding_rate=0.0001,
            volume_24h=12_345_678,
            source="fixture",
        )

    def get_microstructure(self, symbol: str) -> MarketMicrostructure:
        price = 100_000.0 if symbol.upper().startswith("BTC") else 4_000.0
        return MarketMicrostructure(
            symbol=symbol,
            captured_at=int(time() * 1000),
            bid=price - 1,
            ask=price + 1,
            spread_bps=(2 / price) * 10_000,
            depth_imbalance=0.12,
            taker_buy_ratio=0.54,
            funding_rate=0.0001,
            open_interest=140_000.0,
            source="fixture",
        )

    def get_contracts(self):
        return [
            self._contract(
                symbol="BTC-USDT",
                price_precision=1,
                quantity_precision=6,
                contract_value=Decimal("0.000001"),
                min_leverage=1,
                max_leverage=20,
                min_quantity=Decimal("0.0001"),
            )
        ]

    def get_balances(self) -> list[ExchangeBalance]:
        return [
            ExchangeBalance(
                asset="SUSDT",
                balance=Decimal(10000),
                available_balance=Decimal(10000),
                frozen=Decimal(0),
                unrealized_pnl=Decimal(0),
            )
        ]

    def get_positions(self) -> list[ExchangePosition]:
        return []

    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        now = datetime.now(UTC)
        price = request.price or Decimal(100000)
        order = ExchangeOrder(
            order_id=f"fixture-{len(self._orders) + 1}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            position_side=request.position_side,
            status="FILLED",
            order_type=request.order_type,
            quantity=request.quantity,
            executed_quantity=request.quantity,
            price=price,
            average_price=price,
            time_in_force=request.time_in_force,
            created_at=now,
            updated_at=now,
            reduce_only=request.reduce_only,
        )
        self._orders[request.client_order_id] = order
        return order

    def get_order(self, order_id: str) -> ExchangeOrder:
        return next(order for order in self._orders.values() if order.order_id == order_id)

    def get_order_by_client_id(self, client_order_id: str) -> ExchangeOrder | None:
        return self._orders.get(client_order_id)

    def get_trades(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[ExchangeFill]:
        del symbol, order_id, limit
        return []

    @staticmethod
    def _contract(**kwargs):
        from app.exchange.base import ContractInfo

        return ContractInfo(**kwargs)

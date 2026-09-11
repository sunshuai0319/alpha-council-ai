import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Self
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings
from app.domain.schemas import Candle, MarketMicrostructure, MarketSnapshot
from app.exchange.base import (
    ContractInfo,
    ExchangeBalance,
    ExchangeClient,
    ExchangeError,
    ExchangeFill,
    ExchangeOrder,
    ExchangePosition,
    OrderRequest,
)

_CLIENT_ORDER_ID = re.compile(r"^[.A-Za-z0-9_:/-]{1,36}$")


@dataclass(frozen=True)
class WeexCredentials:
    api_key: str
    api_secret: str
    passphrase: str


def normalize_symbol(symbol: str) -> str:
    """Return the application symbol form, e.g. BTCSUSDT/BTCUSDT -> BTC-USDT."""

    value = symbol.upper().replace("-", "").replace("_", "")
    if value.endswith("SUSDT"):
        value = f"{value[:-5]}USDT"
    if value.endswith("USDT") and len(value) > 4:
        return f"{value[:-4]}-USDT"
    return symbol.upper()


def _exchange_symbol(symbol: str, virtual: bool = False) -> str:
    normalized = normalize_symbol(symbol).replace("-", "")
    if virtual and normalized.endswith("USDT"):
        return f"{normalized[:-4]}SUSDT"
    return normalized


def _round_down(value: Decimal, precision: int | None) -> Decimal:
    """向下舍入到合约允许的精度。

    交易所把精度当硬校验：实测数量带 37 位小数、触发价带 3 位小数都会直接回
    500。统一向下取，保证不会放大到超出风控算出来的名义价值。
    """

    if precision is None:
        return value
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _timestamp(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _status(value: str | None) -> str:
    return {
        "NEW": "OPEN",
        "PARTIALLY_FILLED": "PARTIALLY_FILLED",
        "FILLED": "FILLED",
        "CANCELED": "CANCELED",
        "REJECTED": "REJECTED",
    }.get((value or "").upper(), "UNKNOWN")


class WeexClient(ExchangeClient):
    """WEEX Futures V3 adapter with virtual-account isolation by default.

    The client uses the V3 contract API. Public market requests use
    ``/capi/v3/market``; authenticated virtual-account requests use the
    documented ``/capi/v3/sim`` paths.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        clock_ms: Callable[[], int] | None = None,
        credentials: WeexCredentials | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.credentials = credentials
        self._contracts: dict[str, ContractInfo] | None = None
        self._client = client or httpx.Client(
            base_url=self.settings.weex_base_url.rstrip("/"),
            timeout=self.settings.ark_timeout_seconds,
        )
        self._owns_client = client is None
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @property
    def supports_trade_fills(self) -> bool:
        """Whether ``get_trades`` can be used at all.

        The virtual account exposes only balance / position / order(POST) /
        order/history, so there is no fill feed to reconcile against.
        """

        return not self.settings.weex_virtual_only

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def signature(
        secret_key: str,
        timestamp: str,
        method: str,
        request_path: str,
        query_string: str = "",
        body: str = "",
    ) -> str:
        """Build the V3 ACCESS-SIGN value from the exact request bytes."""

        query = f"?{query_string}" if query_string else ""
        message = f"{timestamp}{method.upper()}{request_path}{query}{body}"
        digest = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _auth_headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
    ) -> dict[str, str]:
        if self.credentials is None:
            raise ExchangeError("WEEX private API credentials are not configured")
        timestamp = str(self._clock_ms())
        return {
            "ACCESS-KEY": self.credentials.api_key,
            "ACCESS-SIGN": self.signature(
                self.credentials.api_secret,
                timestamp,
                method,
                path,
                query_string,
                body,
            ),
            "ACCESS-PASSPHRASE": self.credentials.passphrase,
            "ACCESS-TIMESTAMP": timestamp,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        private: bool = False,
    ) -> Any:
        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        query_string = urlencode(clean_params)
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}
        if private:
            headers.update(self._auth_headers(method, path, query_string, body))

        try:
            response = self._client.request(
                method,
                path,
                params=clean_params or None,
                content=body or None,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExchangeError(f"WEEX request failed: {method} {path}: {exc}") from exc

        if isinstance(payload, dict) and payload.get("success") is False:
            message = payload.get("errorMessage") or payload.get("msg") or "request rejected"
            raise ExchangeError(f"WEEX request rejected: {message}")
        return payload

    def _private_path(self, suffix: str) -> str:
        prefix = "/capi/v3/sim" if self.settings.weex_virtual_only else "/capi/v3"
        return f"{prefix}/{suffix.lstrip('/')}"

    @staticmethod
    def parse_candle(
        raw: list[Any],
        symbol: str = "BTC-USDT",
        timeframe: str = "5m",
    ) -> Candle:
        if len(raw) < 6:
            raise ExchangeError("WEEX candle response must contain at least six fields")
        return Candle(
            symbol=normalize_symbol(symbol),
            timeframe=timeframe,
            open_time=int(raw[0]),
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            volume=float(raw[5]),
        )

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        if not 1 <= limit <= 1000:
            raise ValueError("WEEX kline limit must be between 1 and 1000")
        raw = self._request(
            "GET",
            "/capi/v3/market/klines",
            params={"symbol": _exchange_symbol(symbol), "interval": timeframe, "limit": limit},
        )
        if not isinstance(raw, list):
            raise ExchangeError("WEEX kline response is not an array")
        return [self.parse_candle(item, symbol, timeframe) for item in raw]

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        raw = self._request(
            "GET",
            "/capi/v3/market/ticker/24hr",
            params={"symbol": _exchange_symbol(symbol)},
        )
        ticker = raw[0] if isinstance(raw, list) and raw else raw
        if not isinstance(ticker, dict):
            raise ExchangeError("WEEX ticker response is not an object")
        # 24h ticker 不提供「本次报价时间」：closeTime 是 24h 滚动窗口的边界，
        # 实测比当前时刻落后约 12 分钟。拿它当 captured_at 会让
        # market_data_max_age_seconds(默认 90s) 永远判定过期，整个系统一笔都不下。
        # 所以 captured_at 取本地观测时刻 —— 行情就是此刻从交易所取回的。
        captured_at = self._clock_ms()

        def optional(name: str) -> float | None:
            """缺字段与「字段存在但为空」要区分开，所以只对存在且有值的做转换。"""

            raw_value = ticker.get(name)
            return float(_decimal(raw_value)) if raw_value is not None else None

        return MarketSnapshot(
            symbol=normalize_symbol(ticker.get("symbol", symbol)),
            captured_at=captured_at,
            last_price=float(_decimal(ticker.get("lastPrice", ticker.get("last")))),
            bid=optional("bidPrice") or optional("best_bid"),
            ask=optional("askPrice") or optional("best_ask"),
            volume_24h=optional("volume") or optional("volume_24h"),
            open_24h=optional("openPrice"),
            high_24h=optional("highPrice"),
            low_24h=optional("lowPrice"),
            price_change_pct=optional("priceChangePercent"),
            quote_volume_24h=optional("quoteVolume"),
            mark_price=optional("markPrice"),
            index_price=optional("indexPrice"),
        )

    def get_microstructure(self, symbol: str) -> MarketMicrostructure:
        """盘口 / 成交流水 / 资金费率 / 持仓量。

        虚拟盘上这四个端点都可用（见 docs/weex-virtual-api.md）。symbol 必须是不带
        横杠的合约符号，否则返回 -1142。

        注意：这些数值疑似合成（实测价差低到 0.00013%），只可作辅助确认项。
        """

        contract_symbol = _exchange_symbol(symbol)
        depth = self._request("GET", "/capi/v3/market/depth", params={"symbol": contract_symbol})
        trades = self._request("GET", "/capi/v3/market/trades", params={"symbol": contract_symbol})
        funding = self._request("GET", "/capi/v3/market/fundingRate", params={"symbol": contract_symbol})
        interest = self._request("GET", "/capi/v3/market/openInterest", params={"symbol": contract_symbol})

        bids = [(float(price), float(size)) for price, size in (depth.get("bids") or [])[:5]]
        asks = [(float(price), float(size)) for price, size in (depth.get("asks") or [])[:5]]
        bid = bids[0][0] if bids else None
        ask = asks[0][0] if asks else None
        bid_volume = sum(size for _, size in bids)
        ask_volume = sum(size for _, size in asks)
        buy_volume = sum(float(item["qty"]) for item in trades if not item.get("isBuyerMaker"))
        total_volume = sum(float(item["qty"]) for item in trades)
        return MarketMicrostructure(
            symbol=normalize_symbol(symbol),
            captured_at=self._clock_ms(),
            bid=bid,
            ask=ask,
            spread_bps=((ask - bid) / bid * 10_000) if bid and ask and bid > 0 else None,
            depth_imbalance=(
                (bid_volume - ask_volume) / (bid_volume + ask_volume)
                if (bid_volume + ask_volume) > 0
                else None
            ),
            taker_buy_ratio=(buy_volume / total_volume) if total_volume > 0 else None,
            funding_rate=float(funding[0]["fundingRate"]) if funding else None,
            open_interest=float(interest["openInterest"]) if interest else None,
        )

    def get_contracts(self) -> list[ContractInfo]:
        return list(self._contract_index().values())

    def _contract_index(self) -> dict[str, ContractInfo]:
        """按符号索引的合约规格，缓存一次。

        下单要用它做精度舍入，不能每单都重新拉一遍 exchangeInfo。
        """

        if self._contracts is None:
            raw = self._request("GET", "/capi/v3/market/exchangeInfo")
            symbols = raw.get("symbols", []) if isinstance(raw, dict) else raw
            if not isinstance(symbols, list):
                raise ExchangeError("WEEX exchangeInfo response has no symbols array")
            self._contracts = {
                normalize_symbol(item["symbol"]): ContractInfo(
                    symbol=normalize_symbol(item["symbol"]),
                    price_precision=int(item.get("pricePrecision", 0)),
                    quantity_precision=int(item.get("quantityPrecision", 0)),
                    contract_value=_decimal(item.get("contractVal")),
                    min_leverage=int(item.get("minLeverage", 1)),
                    max_leverage=int(item.get("maxLeverage", 1)),
                    min_quantity=_decimal(item.get("minOrderSize")),
                    max_quantity=_decimal(item["maxOrderSize"]) if item.get("maxOrderSize") else None,
                )
                for item in symbols
            }
        return self._contracts

    def get_balances(self) -> list[ExchangeBalance]:
        raw = self._request("GET", self._private_path("balance"), private=True)
        if not isinstance(raw, list):
            raise ExchangeError("WEEX balance response is not an array")
        return [
            ExchangeBalance(
                asset=str(item["asset"]),
                balance=_decimal(item.get("balance")),
                available_balance=_decimal(item.get("availableBalance")),
                frozen=_decimal(item.get("frozen")),
                unrealized_pnl=_decimal(item.get("unrealizePnl")),
                raw=item,
            )
            for item in raw
        ]

    def get_positions(self) -> list[ExchangePosition]:
        raw = self._request("GET", self._private_path("position/allPosition"), private=True)
        if not isinstance(raw, list):
            raise ExchangeError("WEEX position response is not an array")
        return [
            ExchangePosition(
                position_id=str(item.get("id", "")),
                symbol=normalize_symbol(item["symbol"]),
                side=str(item["side"]).upper(),
                quantity=_decimal(item.get("size")),
                entry_value=_decimal(item.get("openValue")),
                margin=_decimal(item.get("marginSize")),
                leverage=int(_decimal(item.get("leverage"), "1")),
                unrealized_pnl=_decimal(item.get("unrealizePnl")),
                liquidation_price=(
                    _decimal(item.get("liquidatePrice"))
                    if _decimal(item.get("liquidatePrice")) != 0
                    else None
                ),
                raw=item,
            )
            for item in raw
        ]

    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        if not _CLIENT_ORDER_ID.fullmatch(request.client_order_id):
            raise ValueError("WEEX client_order_id must match the V3 character and length rules")
        order_type = request.order_type.upper()
        if order_type == "LIMIT" and request.price is None:
            raise ValueError("WEEX limit orders require a price")
        contract = self._contract_index().get(normalize_symbol(request.symbol))
        quantity = _round_down(request.quantity, contract.quantity_precision if contract else None)
        if contract is not None and contract.min_quantity > 0 and quantity < contract.min_quantity:
            raise ExchangeError(
                f"WEEX order quantity {quantity} for {request.symbol} is below the minimum "
                f"{contract.min_quantity}"
            )
        body: dict[str, Any] = {
            "symbol": _exchange_symbol(request.symbol, self.settings.weex_virtual_only),
            "side": request.side.upper(),
            "positionSide": request.position_side.upper(),
            "type": order_type,
            "quantity": str(quantity),
            "newClientOrderId": request.client_order_id,
        }
        # reduceOnly 仅正式合约 API 文档定义；模拟盘 /capi/v3/sim/order 未定义该参数，不发送。
        if not self.settings.weex_virtual_only:
            body["reduceOnly"] = request.reduce_only
        if request.time_in_force is not None:
            body["timeInForce"] = request.time_in_force.upper()
        price_precision = contract.price_precision if contract else None
        if request.price is not None:
            body["price"] = str(_round_down(request.price, price_precision))
        if request.take_profit is not None:
            body["tpTriggerPrice"] = str(_round_down(request.take_profit, price_precision))
        if request.stop_loss is not None:
            body["slTriggerPrice"] = str(_round_down(request.stop_loss, price_precision))
        raw = self._request("POST", self._private_path("order"), json_body=body, private=True)
        if not isinstance(raw, dict) or not raw.get("success", False):
            message = raw.get("errorMessage", "request rejected") if isinstance(raw, dict) else "invalid response"
            raise ExchangeError(f"WEEX order rejected: {message}")
        accepted = ExchangeOrder(
            order_id=str(raw["orderId"]),
            client_order_id=str(raw.get("clientOrderId", request.client_order_id)),
            symbol=normalize_symbol(request.symbol),
            side=request.side.upper(),
            position_side=request.position_side.upper(),
            status="OPEN",
            order_type=order_type,
            quantity=request.quantity,
            executed_quantity=Decimal(0),
            price=request.price or Decimal(0),
            average_price=Decimal(0),
            time_in_force=request.time_in_force,
            created_at=None,
            updated_at=None,
            reduce_only=request.reduce_only,
            raw=raw,
        )
        return self._refresh_order(accepted)

    def _refresh_order(self, accepted: ExchangeOrder) -> ExchangeOrder:
        """Fill in the real status the order response omits.

        下单响应只有 orderId/clientOrderId/success，没有 status；市价单实测在响应
        返回后立刻可查到终态。回查失败说明状态未知，退回 ``OPEN`` —— 订单已受理，
        不能因为补充查询失败就让调用方以为下单失败。
        """

        try:
            current = self.get_order_by_client_id(accepted.client_order_id)
        except ExchangeError:
            return accepted
        return current if current is not None else accepted

    def _parse_order(self, raw: dict[str, Any]) -> ExchangeOrder:
        return ExchangeOrder(
            order_id=str(raw["orderId"]),
            client_order_id=str(raw.get("clientOrderId", "")),
            symbol=normalize_symbol(raw["symbol"]),
            side=str(raw["side"]).upper(),
            position_side=str(raw.get("positionSide", "")).upper(),
            status=_status(raw.get("status")),
            order_type=str(raw.get("type", "")).upper(),
            quantity=_decimal(raw.get("origQty")),
            executed_quantity=_decimal(raw.get("executedQty")),
            price=_decimal(raw.get("price")),
            average_price=_decimal(raw.get("avgPrice")),
            time_in_force=raw.get("timeInForce"),
            created_at=_timestamp(raw.get("time")),
            updated_at=_timestamp(raw.get("updateTime")),
            reduce_only=bool(raw.get("reduceOnly", False)),
            raw=raw,
        )

    def _order_history(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Query ``/capi/v3[/sim]/order/history``.

        Only closed orders (FILLED or CANCELED) appear here: while a limit
        order is resting it is absent from this feed.
        """

        raw = self._request(
            "GET",
            self._private_path("order/history"),
            params={
                "symbol": _exchange_symbol(symbol, self.settings.weex_virtual_only) if symbol else None,
                "limit": 100,
                "page": 0,
            },
            private=True,
        )
        if not isinstance(raw, list):
            raise ExchangeError("WEEX order history response is not an array")
        return [item for item in raw if isinstance(item, dict)]

    def get_order(self, order_id: str) -> ExchangeOrder:
        if self.settings.weex_virtual_only:
            # 模拟盘没有单笔查单接口（GET /capi/v3/sim/order 返回 405），只能从
            # order/history 反查；该接口仅含终态订单，挂单中的订单查不到。
            for item in self._order_history():
                if str(item.get("orderId")) == str(order_id):
                    return self._parse_order(item)
            raise ExchangeError(
                f"WEEX order {order_id} is not in order history; "
                "the virtual account only exposes closed orders"
            )
        raw = self._request(
            "GET",
            self._private_path("order"),
            params={"orderId": order_id},
            private=True,
        )
        if not isinstance(raw, dict):
            raise ExchangeError("WEEX order response is not an object")
        return self._parse_order(raw)

    def get_order_by_client_id(self, client_order_id: str) -> ExchangeOrder | None:
        for item in self._order_history():
            if item.get("clientOrderId") == client_order_id:
                return self._parse_order(item)
        return None

    def get_trades(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[ExchangeFill]:
        if not 1 <= limit <= 100:
            raise ValueError("WEEX trade limit must be between 1 and 100")
        if self.settings.weex_virtual_only:
            # 模拟盘只开放 balance / position / order(POST) / order/history 四个
            # 端点，没有成交流水。返回 realized_pnl 恒为 0 的假成交会让日亏损与
            # 连亏熔断永远不触发，因此这里直接失败。
            raise ExchangeError(
                "WEEX virtual account does not expose trade fills; "
                "derive realised PnL from balance snapshots instead"
            )
        raw = self._request(
            "GET",
            self._private_path("userTrades"),
            params={
                "symbol": _exchange_symbol(symbol, self.settings.weex_virtual_only) if symbol else None,
                "orderId": order_id,
                "limit": limit,
            },
            private=True,
        )
        if not isinstance(raw, list):
            raise ExchangeError("WEEX trade response is not an array")
        return [
            ExchangeFill(
                fill_id=str(item["id"]),
                order_id=str(item["orderId"]),
                symbol=normalize_symbol(item["symbol"]),
                side=str(item["side"]).upper(),
                position_side=str(item.get("positionSide", "")).upper(),
                quantity=_decimal(item.get("qty")),
                price=_decimal(item.get("price")),
                fee=_decimal(item.get("commission")),
                realized_pnl=_decimal(item.get("realizedPnl")),
                filled_at=_timestamp(item.get("time")) or datetime.now(UTC),
                raw=item,
            )
            for item in raw
        ]

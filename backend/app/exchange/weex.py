import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings
from app.domain.schemas import Candle, MarketSnapshot
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
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client or httpx.Client(
            base_url=self.settings.weex_base_url.rstrip("/"),
            timeout=self.settings.ark_timeout_seconds,
        )
        self._owns_client = client is None
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

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
        if not all(
            (
                self.settings.weex_api_key,
                self.settings.weex_api_secret,
                self.settings.weex_api_passphrase,
            )
        ):
            raise ExchangeError("WEEX private API credentials are not configured")
        timestamp = str(self._clock_ms())
        return {
            "ACCESS-KEY": self.settings.weex_api_key,
            "ACCESS-SIGN": self.signature(
                self.settings.weex_api_secret,
                timestamp,
                method,
                path,
                query_string,
                body,
            ),
            "ACCESS-PASSPHRASE": self.settings.weex_api_passphrase,
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
        captured_at = int(ticker.get("closeTime") or ticker.get("timestamp") or self._clock_ms())
        return MarketSnapshot(
            symbol=normalize_symbol(ticker.get("symbol", symbol)),
            captured_at=captured_at,
            last_price=float(_decimal(ticker.get("lastPrice", ticker.get("last")))),
            bid=float(_decimal(ticker.get("bidPrice", ticker.get("best_bid"))))
            if ticker.get("bidPrice", ticker.get("best_bid")) is not None
            else None,
            ask=float(_decimal(ticker.get("askPrice", ticker.get("best_ask"))))
            if ticker.get("askPrice", ticker.get("best_ask")) is not None
            else None,
            volume_24h=float(_decimal(ticker.get("volume", ticker.get("volume_24h"))))
            if ticker.get("volume", ticker.get("volume_24h")) is not None
            else None,
        )

    def get_contracts(self) -> list[ContractInfo]:
        raw = self._request("GET", "/capi/v3/market/exchangeInfo")
        symbols = raw.get("symbols", []) if isinstance(raw, dict) else raw
        if not isinstance(symbols, list):
            raise ExchangeError("WEEX exchangeInfo response has no symbols array")
        return [
            ContractInfo(
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
        ]

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
        body: dict[str, Any] = {
            "symbol": _exchange_symbol(request.symbol, self.settings.weex_virtual_only),
            "side": request.side.upper(),
            "positionSide": request.position_side.upper(),
            "type": order_type,
            "quantity": str(request.quantity),
            "newClientOrderId": request.client_order_id,
            "reduceOnly": request.reduce_only,
        }
        if request.time_in_force is not None:
            body["timeInForce"] = request.time_in_force.upper()
        if request.price is not None:
            body["price"] = str(request.price)
        if request.take_profit is not None:
            body["tpTriggerPrice"] = str(request.take_profit)
        if request.stop_loss is not None:
            body["slTriggerPrice"] = str(request.stop_loss)
        raw = self._request("POST", self._private_path("order"), json_body=body, private=True)
        if not isinstance(raw, dict) or not raw.get("success", False):
            message = raw.get("errorMessage", "request rejected") if isinstance(raw, dict) else "invalid response"
            raise ExchangeError(f"WEEX order rejected: {message}")
        return ExchangeOrder(
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

    def get_order(self, order_id: str) -> ExchangeOrder:
        raw = self._request(
            "GET",
            self._private_path("order"),
            params={"orderId": order_id},
            private=True,
        )
        if not isinstance(raw, dict):
            raise ExchangeError("WEEX order response is not an object")
        return self._parse_order(raw)

    def get_trades(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[ExchangeFill]:
        if not 1 <= limit <= 100:
            raise ValueError("WEEX trade limit must be between 1 and 100")
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

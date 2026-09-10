import hashlib
import hmac
import json
from base64 import b64encode
from decimal import Decimal

import httpx

from app.config import Settings
from app.exchange.base import OrderRequest
from app.exchange.fixtures import (
    CONTRACT_INFO_RESPONSE,
    DEMO_BALANCE_RESPONSE,
    DEMO_POSITION_RESPONSE,
    KLINES_RESPONSE,
    ORDER_ACCEPTED_RESPONSE,
    ORDER_INFO_RESPONSE,
    TICKER_RESPONSE,
    TRADE_RESPONSE,
)
from app.exchange.weex import WeexClient


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        WEEX_API_KEY="key",
        WEEX_API_SECRET="secret",
        WEEX_API_PASSPHRASE="passphrase",
        WEEX_VIRTUAL_ONLY=True,
    )


def test_weex_signature_matches_v3_documented_algorithm() -> None:
    timestamp = "1561022985382"
    body = '{"symbol":"BTCUSDT","side":"BUY","type":"LIMIT","timeInForce":"GTC","quantity":"1","price":"68900","newClientOrderId":"my-order-001"}'
    message = f"{timestamp}POST/api/v3/order{body}"
    expected = b64encode(hmac.new(b"secret", message.encode(), hashlib.sha256).digest()).decode()
    assert WeexClient.signature("secret", timestamp, "POST", "/api/v3/order", body=body) == expected


def test_weex_candle_response_maps_to_candle() -> None:
    candle = WeexClient.parse_candle([1700000000000, "1", "2", "0.5", "1.5", "10"])
    assert candle.symbol == "BTC-USDT"
    assert candle.timeframe == "5m"
    assert candle.close == 1.5


def test_weex_virtual_requests_use_demo_paths_and_symbols() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/klines":
            return httpx.Response(200, json=KLINES_RESPONSE)
        if request.url.path == "/capi/v3/market/ticker/24hr":
            return httpx.Response(200, json=TICKER_RESPONSE)
        if request.url.path == "/capi/v3/market/exchangeInfo":
            return httpx.Response(200, json=CONTRACT_INFO_RESPONSE)
        if request.url.path == "/capi/v3/sim/balance":
            return httpx.Response(200, json=DEMO_BALANCE_RESPONSE)
        if request.url.path == "/capi/v3/sim/position/allPosition":
            return httpx.Response(200, json=DEMO_POSITION_RESPONSE)
        if request.url.path == "/capi/v3/sim/order" and request.method == "POST":
            assert json.loads(request.content)["symbol"] == "BTCSUSDT"
            return httpx.Response(200, json=ORDER_ACCEPTED_RESPONSE)
        if request.url.path == "/capi/v3/sim/order" and request.method == "GET":
            return httpx.Response(200, json=ORDER_INFO_RESPONSE)
        if request.url.path == "/capi/v3/sim/userTrades":
            return httpx.Response(200, json=TRADE_RESPONSE)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(base_url="https://api-contract.weex.com", transport=transport) as http_client, WeexClient(
        _settings(), client=http_client, clock_ms=lambda: 1700000000000
    ) as client:
        assert client.get_candles("BTC-USDT", "5m")[0].close == 1.5
        assert client.get_market_snapshot("BTC-USDT").symbol == "BTC-USDT"
        assert client.get_contracts()[0].max_leverage == 408
        assert client.get_balances()[0].asset == "SUSDT"
        assert client.get_positions()[0].symbol == "BTC-USDT"
        order = client.place_order(
            OrderRequest(
                symbol="BTC-USDT",
                side="BUY",
                position_side="LONG",
                order_type="MARKET",
                quantity=Decimal("0.01"),
                client_order_id="alpha-0001",
            )
        )
        assert order.status == "OPEN"
        assert client.get_order(order.order_id).status == "FILLED"
        assert client.get_trades("BTC-USDT")[0].realized_pnl == 0

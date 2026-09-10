import hashlib
import hmac
import json
from base64 import b64encode
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.exchange.base import ExchangeError, OrderRequest
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
from app.exchange.weex import WeexClient, WeexCredentials


def _settings(virtual_only: bool = True) -> Settings:
    return Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        WEEX_VIRTUAL_ONLY=virtual_only,
    )


def test_weex_signature_matches_v3_documented_algorithm() -> None:
    timestamp = "1561022985382"
    body = '{"symbol":"BTCUSDT","side":"BUY","type":"LIMIT","timeInForce":"GTC","quantity":"1","price":"68900","newClientOrderId":"my-order-001"}'
    message = f"{timestamp}POST/api/v3/order{body}"
    expected = b64encode(hmac.new(b"secret", message.encode(), hashlib.sha256).digest()).decode()
    assert WeexClient.signature("secret", timestamp, "POST", "/api/v3/order", body=body) == expected


def test_weex_private_requests_use_explicit_account_credentials() -> None:
    settings = _settings()
    client = WeexClient(
        settings,
        credentials=WeexCredentials(
            api_key="account-key",
            api_secret="account-secret",
            passphrase="account-passphrase",
        ),
        clock_ms=lambda: 1700000000000,
    )

    headers = client._auth_headers("GET", "/capi/v3/sim/balance")

    assert headers["ACCESS-KEY"] == "account-key"
    assert headers["ACCESS-PASSPHRASE"] == "account-passphrase"
    assert headers["ACCESS-SIGN"] == WeexClient.signature(
        "account-secret",
        "1700000000000",
        "GET",
        "/capi/v3/sim/balance",
    )


def test_weex_private_requests_do_not_fallback_to_settings() -> None:
    client = WeexClient(_settings())

    with pytest.raises(ExchangeError, match="credentials are not configured"):
        client._auth_headers("GET", "/capi/v3/sim/balance")


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
            body = json.loads(request.content)
            assert body["symbol"] == "BTCSUSDT"
            assert "reduceOnly" not in body  # 模拟盘文档未定义该参数
            return httpx.Response(200, json=ORDER_ACCEPTED_RESPONSE)
        if request.url.path == "/capi/v3/sim/order/history":
            return httpx.Response(200, json=[ORDER_INFO_RESPONSE])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(base_url="https://api-contract.weex.com", transport=transport) as http_client, WeexClient(
        _settings(),
        client=http_client,
        clock_ms=lambda: 1700000000000,
        credentials=WeexCredentials("key", "secret", "passphrase"),
    ) as client:
        assert client.get_candles("BTC-USDT", "5m")[0].close == 1.5
        assert client.get_market_snapshot("BTC-USDT").symbol == "BTC-USDT"
        assert client.get_contracts()[0].max_leverage == 408
        assert client.get_balances()[0].asset == "SUSDT"
        assert client.get_positions()[0].symbol == "BTC-USDT"
        order = _place_market(client)
        assert order.status == "FILLED"
        assert client.get_order(order.order_id).status == "FILLED"


def _client_for(handler, virtual_only: bool = True) -> WeexClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api-contract.weex.com", transport=transport)
    return WeexClient(
        _settings(virtual_only),
        client=http_client,
        clock_ms=lambda: 1700000000000,
        credentials=WeexCredentials("key", "secret", "passphrase"),
    )


def _precision_client(handler) -> WeexClient:
    return _client_for(handler)


def test_weex_place_order_rounds_quantity_and_prices_to_contract_precision() -> None:
    """交易所对精度是硬要求：实测未舍入的数量/触发价都被回 500。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/exchangeInfo":
            return httpx.Response(200, json=CONTRACT_INFO_RESPONSE)
        if request.url.path == "/capi/v3/sim/order" and request.method == "POST":
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=ORDER_ACCEPTED_RESPONSE)
        if request.url.path == "/capi/v3/sim/order/history":
            return httpx.Response(200, json=[ORDER_INFO_RESPONSE])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _precision_client(handler) as client:
        client.place_order(
            OrderRequest(
                symbol="BTC-USDT",
                side="BUY",
                position_side="LONG",
                order_type="LIMIT",
                quantity=Decimal("0.02600207089941456146714562637"),
                client_order_id="alpha-0001",
                price=Decimal("76940.123456"),
                time_in_force="GTC",
                stop_loss=Decimal("76140.999"),
            )
        )

    # CONTRACT_INFO_RESPONSE: quantityPrecision=6, pricePrecision=1
    assert captured["quantity"] == "0.026002"
    assert captured["price"] == "76940.1"
    assert captured["slTriggerPrice"] == "76140.9"


def test_weex_place_order_rejects_quantity_below_contract_minimum() -> None:
    """低于最小下单量要当场说清楚，而不是让交易所回一个 500。"""
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/exchangeInfo":
            return httpx.Response(200, json=CONTRACT_INFO_RESPONSE)
        posted.append(request.url.path)
        return httpx.Response(200, json=ORDER_ACCEPTED_RESPONSE)

    with _precision_client(handler) as client, pytest.raises(ExchangeError, match="below the minimum"):
        client.place_order(
            OrderRequest(
                symbol="BTC-USDT",
                side="BUY",
                position_side="LONG",
                order_type="MARKET",
                quantity=Decimal("0.00000001"),
                client_order_id="alpha-0001",
            )
        )

    assert posted == []


def test_weex_snapshot_is_stamped_with_observation_time() -> None:
    """24h ticker 没有「本次报价时间」。

    实测 closeTime 是 24h 滚动窗口边界，比当前时间落后约 12 分钟。用它当
    captured_at 会让 market_data_max_age_seconds(90s) 永远判定过期，全系统
    一笔都不下。因此 captured_at 必须取本地观测时刻。
    """
    stale_close_time = 1_700_000_000_000 - 12 * 60 * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/ticker/24hr":
            return httpx.Response(200, json=[{**TICKER_RESPONSE, "closeTime": stale_close_time}])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler) as client:
        snapshot = client.get_market_snapshot("BTC-USDT")

    assert snapshot.captured_at == 1_700_000_000_000
    assert snapshot.last_price == 1.5


def test_weex_virtual_get_order_reads_from_order_history() -> None:
    """虚拟盘没有 GET /sim/order（实测 405），只能从 order/history 反查终态订单。"""
    requested: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        if request.url.path == "/capi/v3/sim/order/history":
            return httpx.Response(200, json=[ORDER_INFO_RESPONSE])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler) as client:
        order = client.get_order("702345678901234567")

    assert order.status == "FILLED"
    assert ("GET", "/capi/v3/sim/order") not in requested


def test_weex_virtual_get_order_rejects_order_missing_from_history() -> None:
    """挂单不在 order/history 里（实测只返回终态订单），必须报错而不是返回假数据。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with _client_for(handler) as client, pytest.raises(ExchangeError, match="not in order history"):
        client.get_order("702345678901234567")


def test_weex_virtual_get_trades_reports_missing_endpoint() -> None:
    """模拟盘没有成交流水接口（GET /capi/v3/sim/userTrades 实测 404）。

    宁可显式报错，也不要返回 realized_pnl 恒为 0 的假成交：静默的 0 会让
    MAX_DAILY_LOSS_PCT / MAX_CONSECUTIVE_LOSSES 熔断永远不触发。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler) as client, pytest.raises(
        ExchangeError, match="does not expose trade fills"
    ):
        client.get_trades("BTC-USDT")


def test_weex_declares_whether_trade_fills_are_available() -> None:
    """对账层据此决定是否拉成交流水，避免用异常做能力探测。"""
    assert WeexClient(_settings()).supports_trade_fills is False
    assert WeexClient(_settings(virtual_only=False)).supports_trade_fills is True


def test_weex_real_get_trades_uses_user_trades_path() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/capi/v3/userTrades":
            return httpx.Response(200, json=TRADE_RESPONSE)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler, virtual_only=False) as client:
        fills = client.get_trades("BTC-USDT")

    assert requested == ["/capi/v3/userTrades"]
    assert fills[0].realized_pnl == Decimal(0)


def _place_market(client: WeexClient, client_order_id: str = "alpha-0001"):
    return client.place_order(
        OrderRequest(
            symbol="BTC-USDT",
            side="BUY",
            position_side="LONG",
            order_type="MARKET",
            quantity=Decimal("0.01"),
            client_order_id=client_order_id,
        )
    )


def test_weex_virtual_place_order_reports_exchange_status() -> None:
    """下单响应只有 orderId/clientOrderId，没有 status（实测）。市价单返回后 T+0
    即可查到 FILLED，所以必须回查，不能写死 OPEN。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/exchangeInfo":
            return httpx.Response(200, json=CONTRACT_INFO_RESPONSE)
        if request.url.path == "/capi/v3/sim/order" and request.method == "POST":
            return httpx.Response(200, json=ORDER_ACCEPTED_RESPONSE)
        if request.url.path == "/capi/v3/sim/order/history":
            return httpx.Response(200, json=[ORDER_INFO_RESPONSE])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler) as client:
        order = _place_market(client)

    assert order.status == "FILLED"
    assert order.average_price == Decimal("1.5")
    assert order.executed_quantity == Decimal("0.01")


def test_weex_virtual_place_order_falls_back_to_open_when_status_unavailable() -> None:
    """订单已受理；回查失败不能让下单本身失败，但要退回 OPEN。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/exchangeInfo":
            return httpx.Response(200, json=CONTRACT_INFO_RESPONSE)
        if request.url.path == "/capi/v3/sim/order" and request.method == "POST":
            return httpx.Response(200, json=ORDER_ACCEPTED_RESPONSE)
        return httpx.Response(500, json={"message": "history unavailable"})

    with _client_for(handler) as client:
        order = _place_market(client)

    assert order.status == "OPEN"


def test_weex_real_order_path_includes_reduce_only() -> None:
    """正式合约 API 文档定义了 reduceOnly，正式路径应发送它。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/exchangeInfo":
            return httpx.Response(200, json=CONTRACT_INFO_RESPONSE)
        if request.url.path == "/capi/v3/order" and request.method == "POST":
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=ORDER_ACCEPTED_RESPONSE)
        if request.url.path == "/capi/v3/order/history":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler, virtual_only=False) as client:
        client.place_order(
            OrderRequest(
                symbol="BTC-USDT",
                side="BUY",
                position_side="LONG",
                order_type="MARKET",
                quantity=Decimal("0.01"),
                client_order_id="alpha-0002",
                reduce_only=True,
            )
        )

    assert captured["symbol"] == "BTCUSDT"
    assert captured["reduceOnly"] is True

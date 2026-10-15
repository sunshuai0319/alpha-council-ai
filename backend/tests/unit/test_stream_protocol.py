"""WEEX 私有 WebSocket 协议层。纯函数，不碰网络。"""

import base64
import hashlib
import hmac
import json
from decimal import Decimal

from app.streams.protocol import (
    ACK,
    CONNECTED,
    EVENT,
    PING,
    UNKNOWN,
    AccountSample,
    FillEvent,
    OrderEvent,
    classify,
    exit_reason,
    is_system_order,
    net_pnl,
    parse_event,
    pong_message,
    subscribe_message,
    ws_auth_headers,
    ws_signature,
)


def test_ws_signature_uses_timestamp_plus_request_path():
    """签名串是 timestamp + /v3/ws/private，**不含** method / query / body。

    这里用一段独立实现交叉验证。真正的回归防线是下面那条「与 REST 签名不同」——
    如果有人把它改成复用 WeexClient.signature，那条会立刻红。
    """

    secret = "test-secret"
    timestamp = "1700000000000"
    expected = base64.b64encode(
        hmac.new(
            secret.encode(), f"{timestamp}/v3/ws/private".encode(), hashlib.sha256
        ).digest()
    ).decode()

    assert ws_signature(secret, timestamp) == expected


def test_ws_signature_differs_from_rest_signature():
    """REST 是 timestamp+method+path+?query+body，两者不可能相等。

    实测踩过：直接复用 REST 的签名串连 WS 会鉴权失败。
    """

    from app.exchange.weex import WeexClient

    secret = "test-secret"
    timestamp = "1700000000000"
    rest = WeexClient.signature(secret, timestamp, "GET", "/v3/ws/private", "", "")

    assert ws_signature(secret, timestamp) != rest


def test_ws_auth_headers_carry_the_documented_keys():
    headers = ws_auth_headers(
        api_key="key-1", api_secret="secret-1", passphrase="pass-1", timestamp_ms=1700000000000
    )

    assert headers["ACCESS-KEY"] == "key-1"
    assert headers["ACCESS-PASSPHRASE"] == "pass-1"
    assert headers["ACCESS-TIMESTAMP"] == "1700000000000"
    assert headers["ACCESS-SIGN"] == ws_signature("secret-1", "1700000000000")


def test_classify_recognises_ping_by_type_field():
    """私有频道的 ping 用 type 字段；公有频道才用 event。两个都要认。"""

    assert classify(json.dumps({"type": "ping", "time": "1"})).kind == PING
    assert classify(json.dumps({"event": "ping", "time": "1"})).kind == PING


def test_classify_recognises_connected_and_ack():
    assert classify(json.dumps({"event": "connected", "cid": "x"})).kind == CONNECTED
    assert classify(json.dumps({"result": True, "id": 1})).kind == ACK


def test_classify_recognises_business_event():
    incoming = classify(json.dumps({"e": "fill", "E": 1, "v": 2, "d": []}))

    assert incoming.kind == EVENT
    assert incoming.payload["e"] == "fill"


def test_classify_treats_malformed_input_as_unknown_without_raising():
    """监听进程不该因为一条畸形消息整体崩掉。"""

    assert classify("not json").kind == UNKNOWN
    assert classify("[1, 2, 3]").kind == UNKNOWN
    assert classify(json.dumps({"nothing": "useful"})).kind == UNKNOWN


def test_pong_and_subscribe_message_shapes():
    assert json.loads(pong_message()) == {"method": "PONG", "id": 1}
    assert json.loads(subscribe_message("fill", 3)) == {
        "method": "SUBSCRIBE",
        "params": ["fill"],
        "id": 3,
    }


# --- 推送映射 ---
#
# 下面三个 ROW 都是 2026-10-15 在虚拟盘上实测抓到的真实推送，字段一个不多一个不少。
# 注意 FILL_ROW **没有 symbol** —— 实测就是这样，只有 coin 与 orderId。

FILL_ROW = {
    "id": "805594661539283038",
    "coin": "SUSDT",
    "orderId": "805594661514117214",
    "marginMode": "CROSSED",
    "separatedMode": "COMBINED",
    "separatedOpenOrderId": "0",
    "positionSide": "SHORT",
    "orderSide": "BUY",
    "fillSize": "100",
    "fillValue": "7.99000",
    "fillFee": "0.00639200",
    "liquidateFee": "0",
    "realizePnl": "0.01000000",
    "direction": "TAKER",
    "createdTime": "1792069086664",
    "updatedTime": "1792069086664",
}

ORDER_ROW = {
    "id": "805594661514117214",
    "coin": "SUSDT",
    "symbol": "DOGESUSDT",
    "positionSide": "SHORT",
    "orderSide": "BUY",
    "price": "0",
    "size": "100",
    "clientOrderId": "4b73a61f-483f-457f-9a24-be0e9918cbe4",
    "type": "TAKE_PROFIT_MARKET",
    "timeInForce": "IOC",
    "reduceOnly": True,
    "triggerPrice": "0.07990",
    "orderSource": "STOP_PROFIT",
    "positionTpsl": True,
    "takerFeeRate": "0.0008",
    "makerFeeRate": "0.0002",
    "status": "FILLED",
    "cumFillSize": "100",
    "cumFillValue": "7.99000",
    "cumFillFee": "0.00639200",
    "cumLiquidateFee": "0",
    "cumRealizePnl": "0.01000000",
    "createdTime": "1792069086664",
    "updatedTime": "1792069086664",
}

ACCOUNT_ROW = {
    "coin": "SUSDT",
    "amount": "59010.73187629",
    "cumDepositAmount": "0",
    "cumWithdrawAmount": "0",
    "cumTransferInAmount": "40000.00000000",
    "cumTransferOutAmount": "0",
    "cumPositionFillFeeAmount": "0.00639200",
    "cumPositionFundingAmount": "-56.35171893",
    "liquidating": False,
}


def test_parse_fill_event():
    events = parse_event({"e": "fill", "E": 1, "v": 703, "d": [FILL_ROW]})

    assert len(events) == 1
    fill = events[0]
    assert isinstance(fill, FillEvent)
    assert fill.exchange_fill_id == "805594661539283038"
    assert fill.exchange_order_id == "805594661514117214"
    assert fill.position_side == "SHORT"
    assert fill.order_side == "BUY"
    assert fill.quantity == Decimal(100)
    assert fill.value == Decimal("7.99000")
    assert fill.fee == Decimal("0.00639200")
    assert fill.realize_pnl == Decimal("0.01000000")
    assert fill.direction == "TAKER"
    assert fill.account_version == 703
    assert fill.filled_at.year == 2026
    # fill 推送里没有 symbol —— 只能靠 orders 频道按 orderId 补。
    assert fill.symbol == ""


def test_parse_order_event():
    events = parse_event({"e": "orders", "E": 1, "v": 703, "d": [ORDER_ROW]})

    order = events[0]
    assert isinstance(order, OrderEvent)
    assert order.symbol == "DOGESUSDT"
    assert order.status == "FILLED"
    assert order.order_source == "STOP_PROFIT"
    assert order.reduce_only is True
    assert order.position_tpsl is True
    assert order.trigger_price == Decimal("0.07990")
    assert order.client_order_id == "4b73a61f-483f-457f-9a24-be0e9918cbe4"
    assert order.cum_realize_pnl == Decimal("0.01000000")


def test_parse_account_sample():
    events = parse_event({"e": "account", "E": 1, "v": 703, "d": [ACCOUNT_ROW]})

    sample = events[0]
    assert isinstance(sample, AccountSample)
    assert sample.coin == "SUSDT"
    assert sample.amount == Decimal("59010.73187629")
    assert sample.cum_transfer_in == Decimal("40000.00000000")
    assert sample.cum_funding_fee == Decimal("-56.35171893")
    assert sample.liquidating is False


def test_parse_event_skips_unknown_channel_and_malformed_rows():
    assert parse_event({"e": "positions", "v": 1, "d": [{"symbol": "20000067"}]}) == []
    assert parse_event({"e": "fill", "v": 1, "d": "not-a-list"}) == []
    assert parse_event({"e": "fill", "v": 1, "d": [{"id": "1"}]}) == []  # 缺字段的行被跳过


def test_net_pnl_subtracts_the_fee():
    """realizePnl **不含手续费** —— 实测止盈 +0.010 而双边费 0.012792，
    净额是 −0.002792。直接拿 realizePnl 当净盈亏会系统性高估。
    """

    fill = parse_event({"e": "fill", "v": 1, "d": [FILL_ROW]})[0]
    assert net_pnl(fill) == Decimal("0.01000000") - Decimal("0.00639200")


def test_exit_reason_from_order_source():
    assert exit_reason(order_source="STOP_PROFIT", reduce_only=True) == "take_profit"
    assert exit_reason(order_source="STOP_LOSS", reduce_only=True) == "stop_loss"
    assert exit_reason(order_source="UNKNOWN_ORDER_SOURCE", reduce_only=True) == "manual"
    assert exit_reason(order_source="WEB", reduce_only=False) == "entry"
    assert exit_reason(
        order_source="UNKNOWN_ORDER_SOURCE", reduce_only=True, cum_liquidate_fee=Decimal("0.5")
    ) == "liquidation"


def test_is_system_order_by_client_id_prefix():
    """系统单是 alpha-<28位hex>，手动单是 UUID v4。"""

    assert is_system_order("alpha-" + "a" * 28) is True
    assert is_system_order("4b73a61f-483f-457f-9a24-be0e9918cbe4") is False
    assert is_system_order("") is False

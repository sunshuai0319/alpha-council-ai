"""WEEX 私有 WebSocket 协议层。纯函数，不碰网络。"""

import base64
import hashlib
import hmac
import json

from app.streams.protocol import (
    ACK,
    CONNECTED,
    EVENT,
    PING,
    UNKNOWN,
    classify,
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

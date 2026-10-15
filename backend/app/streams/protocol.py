"""WEEX 私有 WebSocket 的协议层：签名、消息分类、推送映射。

**全部是纯函数，不做 IO。** socket 那一层薄到只剩 send/recv（见 client.py），
所以这个模块能在没有网络的情况下测完 —— 这是整套采集层可测试性的来源。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

#: 私有频道的 requestPath。签名串是 timestamp + 这个路径，**不含** method / query /
#: body —— 与 REST 的规则不同（app/exchange/weex.py 的 signature），复用会鉴权失败。
PRIVATE_WS_PATH = "/v3/ws/private"

#: v1 订阅三个频道。**不订 positions**：它的 symbol 是内部合约 ID（实测 20000067）
#: 而不是交易对，需要额外维护一份映射；它给的累计费用在 account 频道里有聚合版本。
SUBSCRIBE_CHANNELS: tuple[str, ...] = ("fill", "orders", "account")

#: 消息分类。
PING = "ping"
CONNECTED = "connected"
ACK = "ack"
EVENT = "event"
UNKNOWN = "unknown"


def ws_signature(secret: str, timestamp: str) -> str:
    """``base64(hmac_sha256(secret, timestamp + requestPath))``，见 WEEX WS 文档。"""

    message = f"{timestamp}{PRIVATE_WS_PATH}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def ws_auth_headers(
    *,
    api_key: str,
    api_secret: str,
    passphrase: str,
    timestamp_ms: int,
) -> dict[str, str]:
    """私有频道的握手头。``ACCESS-TIMESTAMP`` 与签名用的 timestamp 必须一致。"""

    timestamp = str(timestamp_ms)
    return {
        "ACCESS-KEY": api_key,
        "ACCESS-PASSPHRASE": passphrase,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-SIGN": ws_signature(api_secret, timestamp),
    }


@dataclass(frozen=True)
class Incoming:
    kind: str
    payload: dict[str, Any]


def classify(raw: str) -> Incoming:
    """把一帧原文分类。

    无法解析成 JSON 对象时归为 ``UNKNOWN`` 而不抛异常 —— 一条畸形消息不该让
    监听进程整体崩掉，调用方按 kind 决定是丢弃还是记录。
    """

    try:
        payload = json.loads(raw)
    except ValueError:
        return Incoming(UNKNOWN, {})
    if not isinstance(payload, dict):
        return Incoming(UNKNOWN, {})
    # 私有频道的 ping 用 type 字段，公有频道用 event —— 两个都认。
    if payload.get("type") == "ping" or payload.get("event") == "ping":
        return Incoming(PING, payload)
    if payload.get("event") == "connected":
        return Incoming(CONNECTED, payload)
    if "result" in payload:
        return Incoming(ACK, payload)
    if payload.get("e"):
        return Incoming(EVENT, payload)
    return Incoming(UNKNOWN, payload)


def pong_message() -> str:
    """服务端 ping 的回执。超过 10 次不回应会被断开。"""

    return json.dumps({"method": "PONG", "id": 1})


def subscribe_message(channel: str, request_id: int) -> str:
    return json.dumps({"method": "SUBSCRIBE", "params": [channel], "id": request_id})

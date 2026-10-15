"""WEEX 私有 WebSocket 的协议层：签名、消息分类、推送映射。

**全部是纯函数，不做 IO。** socket 那一层薄到只剩 send/recv（见 client.py），
所以这个模块能在没有网络的情况下测完 —— 这是整套采集层可测试性的来源。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

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


#: 系统下单的 clientOrderId 前缀（app/execution/service.py 的 stable_client_order_id）。
SYSTEM_CLIENT_ID_PREFIX = "alpha-"


@dataclass(frozen=True)
class FillEvent:
    """``fill`` 频道的一条成交。"""

    exchange_fill_id: str
    exchange_order_id: str
    symbol: str
    position_side: str
    order_side: str
    quantity: Decimal
    value: Decimal
    fee: Decimal
    #: **不含手续费**，净额用 net_pnl()。
    realize_pnl: Decimal
    direction: str
    filled_at: datetime
    account_version: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class OrderEvent:
    """``orders`` 频道的一次委托状态推送。同一订单会推多次（PENDING → FILLED）。"""

    exchange_order_id: str
    client_order_id: str
    symbol: str
    position_side: str
    order_side: str
    order_type: str
    status: str
    order_source: str
    reduce_only: bool
    position_tpsl: bool
    trigger_price: Decimal
    quantity: Decimal
    cum_fill_quantity: Decimal
    cum_fill_value: Decimal
    cum_fill_fee: Decimal
    cum_realize_pnl: Decimal
    cum_liquidate_fee: Decimal
    taker_fee_rate: Decimal
    maker_fee_rate: Decimal
    created_at: datetime
    updated_at: datetime
    account_version: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class AccountSample:
    """``account`` 频道的一次资金快照。"""

    coin: str
    amount: Decimal
    cum_deposit: Decimal
    cum_withdraw: Decimal
    cum_transfer_in: Decimal
    cum_transfer_out: Decimal
    cum_fill_fee: Decimal
    cum_funding_fee: Decimal
    liquidating: bool
    captured_at: datetime
    account_version: int
    raw: dict[str, Any]


StreamEvent = FillEvent | OrderEvent | AccountSample


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _millis(value: Any) -> datetime:
    """毫秒时间戳 → UTC datetime。非法值抛 ValueError，由调用方决定跳过。"""

    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _required(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value in (None, ""):
        raise ValueError(f"missing required field {key!r}")
    return str(value)


def _parse_fill(row: dict[str, Any], version: int) -> FillEvent:
    return FillEvent(
        exchange_fill_id=_required(row, "id"),
        exchange_order_id=_required(row, "orderId"),
        # **fill 推送里没有 symbol**（实测：只有 coin 与 orderId）。留空，
        # 由仓储层用 orders 频道维护的 order_id → symbol 映射补上。
        symbol=str(row.get("symbol") or ""),
        position_side=str(row.get("positionSide") or ""),
        order_side=str(row.get("orderSide") or ""),
        quantity=_decimal(row.get("fillSize")),
        value=_decimal(row.get("fillValue")),
        fee=_decimal(row.get("fillFee")),
        realize_pnl=_decimal(row.get("realizePnl")),
        direction=str(row.get("direction") or ""),
        filled_at=_millis(_required(row, "createdTime")),
        account_version=version,
        raw=row,
    )


def _parse_order(row: dict[str, Any], version: int) -> OrderEvent:
    return OrderEvent(
        exchange_order_id=_required(row, "id"),
        client_order_id=str(row.get("clientOrderId") or ""),
        symbol=str(row.get("symbol") or ""),
        position_side=str(row.get("positionSide") or ""),
        order_side=str(row.get("orderSide") or ""),
        order_type=str(row.get("type") or ""),
        status=str(row.get("status") or ""),
        order_source=str(row.get("orderSource") or ""),
        reduce_only=bool(row.get("reduceOnly")),
        position_tpsl=bool(row.get("positionTpsl")),
        trigger_price=_decimal(row.get("triggerPrice")),
        quantity=_decimal(row.get("size")),
        cum_fill_quantity=_decimal(row.get("cumFillSize")),
        cum_fill_value=_decimal(row.get("cumFillValue")),
        cum_fill_fee=_decimal(row.get("cumFillFee")),
        cum_realize_pnl=_decimal(row.get("cumRealizePnl")),
        cum_liquidate_fee=_decimal(row.get("cumLiquidateFee")),
        taker_fee_rate=_decimal(row.get("takerFeeRate")),
        maker_fee_rate=_decimal(row.get("makerFeeRate")),
        created_at=_millis(_required(row, "createdTime")),
        updated_at=_millis(_required(row, "updatedTime")),
        account_version=version,
        raw=row,
    )


def _parse_account(row: dict[str, Any], version: int) -> AccountSample:
    return AccountSample(
        coin=_required(row, "coin"),
        amount=_decimal(row.get("amount")),
        cum_deposit=_decimal(row.get("cumDepositAmount")),
        cum_withdraw=_decimal(row.get("cumWithdrawAmount")),
        cum_transfer_in=_decimal(row.get("cumTransferInAmount")),
        cum_transfer_out=_decimal(row.get("cumTransferOutAmount")),
        cum_fill_fee=_decimal(row.get("cumPositionFillFeeAmount")),
        cum_funding_fee=_decimal(row.get("cumPositionFundingAmount")),
        liquidating=bool(row.get("liquidating")),
        captured_at=datetime.now(UTC),
        account_version=version,
        raw=row,
    )


#: 频道名 → 行解析器。**positions 不在表里**：它的 symbol 是内部合约 ID，
#: 需要额外映射，v1 不订阅（见模块头的说明）。
_ROW_PARSERS = {
    "fill": _parse_fill,
    "orders": _parse_order,
    "account": _parse_account,
}


def parse_event(payload: dict[str, Any]) -> list[StreamEvent]:
    """把一次推送映射成领域事件。畸形的行**跳过并记日志**，不抛异常。

    单条坏消息不该让监听进程崩掉 —— 那会丢掉整条连接上后续所有事件。
    """

    parser = _ROW_PARSERS.get(str(payload.get("e") or ""))
    rows = payload.get("d")
    if parser is None or not isinstance(rows, list):
        return []
    version = _int(payload.get("v"))
    events: list[StreamEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            events.append(parser(row, version))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            logger.warning("skipping malformed %s row: %s", payload.get("e"), exc)
    return events


def net_pnl(fill: FillEvent) -> Decimal:
    """成交的已实现盈亏净额。

    WEEX 的 ``realizePnl`` **不含手续费**。实测：止盈那笔 ``realizePnl=+0.010``
    而双边费合计 ``0.012792``，净额是 **−0.002792** —— 直接拿 realizePnl 当净盈亏
    会系统性高估，小额交易里尤其致命（0.16% 往返能吃掉全部利润）。
    """

    return fill.realize_pnl - fill.fee


def exit_reason(
    *,
    order_source: str,
    reduce_only: bool,
    cum_liquidate_fee: Decimal = Decimal(0),
) -> str:
    """平仓原因。

    ``orderSource`` 的枚举不统一（实测）：手动开仓是 ``WEB``、手动平仓是
    ``UNKNOWN_ORDER_SOURCE``、条件单是 ``STOP_LOSS`` / ``STOP_PROFIT``。
    所以不能只看它是不是 ``WEB``，要按枚举逐个判。
    """

    if order_source == "STOP_LOSS":
        return "stop_loss"
    if order_source == "STOP_PROFIT":
        return "take_profit"
    if cum_liquidate_fee > 0:
        return "liquidation"
    if reduce_only:
        return "manual"
    return "entry"


def is_system_order(client_order_id: str) -> bool:
    """系统下单的 clientOrderId 是 ``alpha-<28位hex>``，手动单是 UUID v4。"""

    return client_order_id.startswith(SYSTEM_CLIENT_ID_PREFIX)

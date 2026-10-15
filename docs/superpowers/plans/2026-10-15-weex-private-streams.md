# WEEX 私有 WebSocket 采集层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `app/streams/`，通过 WEEX 私有 WebSocket 实时采集用户手动交易的成交、委托与账户资金变动并落库。

**Architecture:** 协议层（签名 / 消息分类 / 推送映射）做成纯函数，socket 层薄到只剩 `send` / `recv`，两者用可注入的 `Transport` 隔开 —— 于是整个模块能在没有网络的情况下测完。常驻进程 `workers/streams.py` 独立于 `workers/scheduler.py` 运行，与交易路径完全解耦。

**Tech Stack:** Python 3.13、`websockets`（已在 `uvicorn[standard]` 依赖里）、SQLAlchemy 2.x、Alembic、pytest。

---

## 背景

`docs/superpowers/specs/2026-10-15-onchain-trade-attestation-design.md` §2.2 与
`docs/weex-virtual-api.md` §2.2 记录了 2026-10-15 的实测：虚拟盘私有 WebSocket
可用，`fill` 频道直接推 `realizePnl` 与 `fillFee`。

当前 `ReconciliationService` 只能从 balance 快照差分推已实现盈亏
（`app/reconciliation/service.py:210`），是账户级的，且 worker 停摆就断档。
本计划把「每笔成交的交易所回报盈亏」变成系统里的一等数据。

**本计划的边界**：只做采集与落库。链上存证（Merkle / IPFS / 合约）不在范围内，
等这一层跑起来积累出真实数据后再单独计划。

### 实测已知的坑（都来自 `probe_private_ws.py` 的真实运行）

1. **签名与 REST 不同**：`base64(hmac_sha256(secret, timestamp + "/v3/ws/private"))`，
   不含 method / query / body。复用 `WeexClient.signature` 会鉴权失败。
2. **`realizePnl` 不含手续费**：实测止盈 `realizePnl=+0.010` 而双边费 `0.012792`，
   净额是 **−0.002792**。直接拿它当净盈亏会系统性高估。
3. **`orderSource` 枚举不统一**：手动开仓 `WEB`、手动平仓 `UNKNOWN_ORDER_SOURCE`、
   条件单 `STOP_LOSS` / `STOP_PROFIT`。
4. **`positions` 频道的 `symbol` 是内部合约 ID**（实测 `20000067`），不是交易对。
   → v1 **不订阅 positions 频道**，避开这个映射问题；它提供的累计费用在 `account`
   频道里有聚合版本，够用。
5. **`account.v` 会跳号**（实测 `696→697→699→701`），**不能当漏推检测**。
6. **服务端 ping 要回 PONG**：私有频道消息体是 `{"type":"ping"}`（公有频道用
   `event` 字段）。超过 10 次不回应会断开。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/app/streams/__init__.py` | 包标记 |
| `backend/app/streams/protocol.py` | 签名、消息分类、推送 → 领域事件。**纯函数，无 IO** |
| `backend/app/streams/client.py` | 异步 WS 客户端：订阅、PONG、重连退避。IO 只有 `Transport` 那一层 |
| `backend/app/streams/repository.py` | 事件落库，按交易所 ID 幂等 |
| `backend/app/streams/listener.py` | 常驻进程：扫描账户 → 每个账户一条连接 → 落库 |
| `backend/app/exchange/credentials.py` | 从 `TradingAccount` 解析凭证（从 `cycle.py` 提取） |
| `backend/workers/streams.py` | 进程入口 |
| `backend/tests/unit/test_stream_protocol.py` | 协议层测试 |
| `backend/tests/unit/test_stream_repository.py` | 仓储层测试 |
| `backend/tests/unit/test_stream_client.py` | 客户端测试（用假 Transport） |
| `backend/tests/unit/test_stream_listener.py` | 监听器目标扫描测试 |

**修改的文件**：`backend/app/config.py`（新增配置）、`backend/app/db/models.py`（三张表）、
`backend/app/services/cycle.py`（改用提取出来的凭证函数）、`docker-compose.yml`（新服务）、
新增一条 Alembic 迁移。

---

## Task 1: 提取凭证解析到 `app/exchange/credentials.py`

`app/services/cycle.py:1025-1045` 的 `_exchange_for_user` 里有一段从
`TradingAccount` 构造 `WeexCredentials` 的逻辑。监听器需要同一段逻辑，但它是私有方法
且绑定在 `TradingCycleService` 上（构造那个服务会连带建 LLM、RAG、Lark 客户端）。
先把它提出来，避免复制粘贴。

**Files:**
- Create: `backend/app/exchange/credentials.py`
- Modify: `backend/app/services/cycle.py:1025-1045`
- Test: `backend/tests/unit/test_exchange_credentials.py`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_exchange_credentials.py`：

```python
"""从 TradingAccount 解析 WEEX 凭证。

这段逻辑原先埋在 TradingCycleService._exchange_for_user 里。监听器也要用它，
但构造那个服务会连带建 LLM / RAG / Lark 客户端 —— 提出来才能单独复用。
"""

from app.db.models import TradingAccount
from app.exchange.credentials import credentials_for_account


def _account(**overrides) -> TradingAccount:
    values = {
        "id": "acct-1",
        "user_id": "u-1",
        "api_key_ref": "key-1",
        "api_secret_ref": "secret-1",
        "passphrase_ref": "pass-1",
    }
    values.update(overrides)
    return TradingAccount(**values)


def test_returns_credentials_when_all_refs_present():
    credentials = credentials_for_account(_account())

    assert credentials is not None
    assert credentials.api_key == "key-1"
    assert credentials.api_secret == "secret-1"
    assert credentials.passphrase == "pass-1"


def test_returns_none_when_account_is_missing():
    assert credentials_for_account(None) is None


def test_returns_none_when_any_ref_is_missing():
    assert credentials_for_account(_account(passphrase_ref=None)) is None


def test_returns_none_for_env_placeholder_refs():
    """`env:` 前缀是历史占位写法，不是真凭证 —— 当成没有。"""

    assert credentials_for_account(_account(api_key_ref="env:WEEX_API_KEY")) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_exchange_credentials.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.exchange.credentials'`

- [ ] **Step 3: 写实现**

创建 `backend/app/exchange/credentials.py`：

```python
"""从 TradingAccount 行解析 WEEX 私有凭证。

原先这段逻辑内联在 ``TradingCycleService._exchange_for_user``（app/services/cycle.py）。
采集层也要用它，而构造 TradingCycleService 会连带建 LLM / RAG / Lark 客户端，
代价太大 —— 提到这里让两边共用同一份规则。
"""

from __future__ import annotations

from app.db.models import TradingAccount
from app.exchange.weex import WeexCredentials

#: 历史占位写法（`env:WEEX_API_KEY`），不是真凭证。
ENV_PLACEHOLDER_PREFIX = "env:"


def credentials_for_account(account: TradingAccount | None) -> WeexCredentials | None:
    """三个引用齐全且都不是占位符时返回凭证，否则 ``None``。

    返回 ``None`` 不是错误：调用方要能区分「这个账户没配凭证」与「凭证错了」，
    前者跳过即可，后者要告警。
    """

    if account is None:
        return None
    refs = (account.api_key_ref, account.api_secret_ref, account.passphrase_ref)
    if not all(refs):
        return None
    if any(ref.startswith(ENV_PLACEHOLDER_PREFIX) for ref in refs):
        return None
    return WeexCredentials(
        api_key=account.api_key_ref,
        api_secret=account.api_secret_ref,
        passphrase=account.passphrase_ref,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_exchange_credentials.py -q`

Expected: PASS（4 passed）

- [ ] **Step 5: 让 `_exchange_for_user` 改用它**

修改 `backend/app/services/cycle.py`，把 1025-1045 行的 `_exchange_for_user` 替换为：

```python
    def _exchange_for_user(self, user_id: str) -> ExchangeClient:
        if self.exchange_factory is not None:
            return self.exchange_factory()
        account = self._account_for_user(user_id)
        credentials = credentials_for_account(account)
        return WeexClient(self.settings, credentials=credentials)
```

并在 `backend/app/services/cycle.py` 的 import 区加一行（按字母序放在其它 `app.exchange` 导入旁边）：

```python
from app.exchange.credentials import credentials_for_account
```

同时删除该文件里现在不再使用的 `WeexCredentials` 导入（如果它没有别的用处 —— 用
`grep -n "WeexCredentials" app/services/cycle.py` 确认）。

- [ ] **Step 6: 跑相关测试确认没有回归**

Run: `cd backend && uv run pytest tests/unit/test_exchange_credentials.py tests/api/test_cycle_service.py -q`

Expected: PASS（全部通过，无新增失败）

- [ ] **Step 7: 静态检查**

Run: `cd backend && uv run ruff check app tests workers`

Expected: `All checks passed!`

- [ ] **Step 8: 提交**

```bash
git add backend/app/exchange/credentials.py backend/tests/unit/test_exchange_credentials.py backend/app/services/cycle.py
git commit -m "refactor(exchange): 提取凭证解析，供采集层复用

_exchange_for_user 里从 TradingAccount 构造 WeexCredentials 的那段逻辑，
监听器也需要。但它是 TradingCycleService 的私有方法，而构造那个服务会
连带建 LLM / RAG / Lark 客户端 —— 代价太大。

提到 app/exchange/credentials.py，两边共用同一份规则。行为不变：
三个引用齐全且都不是 env: 占位符才返回凭证。"
```

---

## Task 2: 新增配置项

**Files:**
- Modify: `backend/app/config.py`（在 `weex_base_url` 附近，约 64 行）
- Test: `backend/tests/unit/test_stream_settings.py`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_stream_settings.py`：

```python
"""采集层配置项的默认值。

默认值就是系统实际会用的值 —— 测试锁住它们，免得改配置时无声地改变运行行为。
"""

from app.config import Settings


def test_stream_settings_defaults():
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )

    assert settings.streams_enabled is True
    assert settings.streams_ws_url == "wss://ws-contract.weex.com/v3/ws/private"
    assert settings.streams_reconnect_initial_seconds == 1.0
    assert settings.streams_reconnect_max_seconds == 60.0
    assert settings.streams_rescan_seconds == 300.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_stream_settings.py -q`

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'streams_enabled'`

- [ ] **Step 3: 写实现**

在 `backend/app/config.py` 里，紧跟 `exchange_take_profit_enabled` 之后加入：

```python
    #: 私有 WebSocket 采集层。关掉后 workers/streams.py 直接退出，不起连接。
    streams_enabled: bool = True
    #: 私有频道端点。虚拟盘凭证被正式合约的 WS 域名接受（实测见
    #: docs/weex-virtual-api.md §2.2）—— 不要换成 sim 前缀，那个路径不存在。
    streams_ws_url: str = "wss://ws-contract.weex.com/v3/ws/private"
    #: 断线重连的指数退避起点与上限（秒）。
    streams_reconnect_initial_seconds: float = 1.0
    streams_reconnect_max_seconds: float = 60.0
    #: 没有可用账户时的重扫间隔（秒）。进程不退出 —— 退出会被 restart 策略
    #: 拉起来变成崩溃循环，日志里只剩重启噪音。
    streams_rescan_seconds: float = 300.0
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_stream_settings.py -q`

Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py backend/tests/unit/test_stream_settings.py
git commit -m "feat(config): 加私有 WS 采集层的配置项

默认开启：这一层的价值就在于持续采集，关着不跑等于没做。端点写死为正式
合约的 WS 域名 —— 实测虚拟盘凭证被它接受，加 sim 前缀的路径不存在。"
```

---

## Task 3: 协议层 —— 签名与消息分类

**Files:**
- Create: `backend/app/streams/__init__.py`
- Create: `backend/app/streams/protocol.py`
- Test: `backend/tests/unit/test_stream_protocol.py`

- [ ] **Step 1: 建包**

创建 `backend/app/streams/__init__.py`，内容为空文件（与 `app/positions/__init__.py` 一致）。

- [ ] **Step 2: 写失败的测试**

创建 `backend/tests/unit/test_stream_protocol.py`：

```python
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
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_stream_protocol.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.streams.protocol'`

- [ ] **Step 4: 写实现**

创建 `backend/app/streams/protocol.py`：

```python
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_stream_protocol.py -q`

Expected: PASS（8 passed）

- [ ] **Step 6: 静态检查**

Run: `cd backend && uv run ruff check app/streams`

Expected: `All checks passed!`

- [ ] **Step 7: 提交**

```bash
git add backend/app/streams/__init__.py backend/app/streams/protocol.py backend/tests/unit/test_stream_protocol.py
git commit -m "feat(streams): 私有 WS 协议层 —— 签名与消息分类

签名串是 timestamp + /v3/ws/private，不含 method/query/body，与 REST 的规则
不同。实测直接复用 WeexClient.signature 会鉴权失败，所以这里单独实现，
并写了一条「两者不相等」的回归测试。

classify 把畸形输入归为 unknown 而不抛异常：一条坏消息不该让监听进程崩掉。"
```

---

## Task 4: 协议层 —— 推送映射为领域事件

**Files:**
- Modify: `backend/app/streams/protocol.py`（追加）
- Test: `backend/tests/unit/test_stream_protocol.py`（追加）

- [ ] **Step 1: 写失败的测试**

先把 `backend/tests/unit/test_stream_protocol.py` 顶部的 import 块**整体替换**成下面这样
（不要在文件中间补 import —— 那会触发 E402，只能用 `# noqa` 压掉）：

```python
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
```

然后在文件末尾追加：

```python
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
    assert fill.quantity == Decimal("100")
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_stream_protocol.py -q`

Expected: FAIL — `ImportError: cannot import name 'FillEvent' from 'app.streams.protocol'`

- [ ] **Step 3: 写实现**

在 `backend/app/streams/protocol.py` 的 import 区补上 `datetime` / `Decimal`：

```python
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
```

并在文件末尾追加：

```python
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
```

同时在文件顶部的 import 之后加 logger：

```python
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_stream_protocol.py -q`

Expected: PASS（15 passed）

- [ ] **Step 5: 静态检查**

Run: `cd backend && ruff check app/streams`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add backend/app/streams/protocol.py backend/tests/unit/test_stream_protocol.py
git commit -m "feat(streams): 推送映射为领域事件

三个频道（fill/orders/account）的推送行 → 冻结 dataclass。positions 不映射：
它的 symbol 是内部合约 ID（实测 20000067）而不是交易对，需要额外维护一份
映射，而它给的累计费用在 account 频道里有聚合版本 —— v1 不值得。

net_pnl() 把「realizePnl 不含手续费」这条写进代码：实测止盈 +0.010 而双边费
0.012792，净额是 −0.002792。直接拿 realizePnl 当净盈亏会系统性高估。

exit_reason() 按 orderSource 枚举逐个判，而不是「是不是 WEB」—— 实测手动
平仓的 orderSource 是 UNKNOWN_ORDER_SOURCE，不是 WEB。"
```

---

## Task 5: 数据模型与迁移

**Files:**
- Modify: `backend/app/db/models.py`（在 `Fill` 之后追加）
- Create: `backend/alembic/versions/012_exchange_stream_events.py`
- Test: `backend/tests/integration/test_migrations.py`（追加）

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/integration/test_migrations.py` 末尾追加：

```python
def test_exchange_stream_tables_exist_after_migration(tmp_path) -> None:
    """采集层的三张表必须由迁移建出来。

    ⚠️ 只改 models.py 是不够的：`001` 的 create_all 只在库处于 001 时跑过一次，
    已迁移的库不会再执行它。新库因为 001 用的是当前模型才碰巧建出来 —— 这个
    假象很容易骗过本地测试。
    """

    url = f"sqlite+pysqlite:///{tmp_path / 'streams.db'}"
    _upgrade(url)

    engine = create_engine(url)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {"exchange_fills", "exchange_order_events", "exchange_account_samples"} <= names
```

> 这个文件里已经有 `_upgrade(url)` 与顶部的 `create_engine` / `inspect` 导入，
> 直接用，不要另造辅助函数。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/integration/test_migrations.py -q`

Expected: FAIL — 三张表不在 `names` 里

- [ ] **Step 3: 加模型**

在 `backend/app/db/models.py` 的 `Fill` 类之后追加：

```python
class ExchangeFillRecord(Base):
    """WS ``fill`` 频道的成交明细。

    这是系统里第一份**交易所回报的单笔盈亏**。`fills` 表是系统自己下单产生的
    成交，与用户手动交易无关；这张表两者都收。
    """

    __tablename__ = "exchange_fills"
    __table_args__ = (
        UniqueConstraint("trading_account_id", "exchange_fill_id", name="uq_exchange_fill"),
        Index("ix_exchange_fills_account_time", "trading_account_id", "filled_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    exchange_fill_id: Mapped[str] = mapped_column(String(128))
    exchange_order_id: Mapped[str] = mapped_column(String(128), index=True)
    #: fill 推送里**没有** symbol，靠 orders 频道按 orderId 补。补不上时留 NULL。
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    position_side: Mapped[str] = mapped_column(String(8), default="")
    order_side: Mapped[str] = mapped_column(String(8), default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    value: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    fee: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    #: ⚠️ **不含手续费**。净额是 realize_pnl − fee，别直接展示这个字段。
    realize_pnl: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    direction: Mapped[str] = mapped_column(String(8), default="")
    account_version: Mapped[int] = mapped_column(Integer, default=0)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ExchangeOrderEvent(Base):
    """WS ``orders`` 频道的委托状态。

    同一订单会推多次（``UNTRIGGERED`` → ``PENDING`` → ``FILLED``），按
    ``exchange_order_id`` 覆盖为最新状态 —— 终态那一行已经带齐了累计字段
    （cumFillValue / cumRealizePnl / triggerPrice），保留中间态没有额外信息。
    """

    __tablename__ = "exchange_order_events"
    __table_args__ = (
        UniqueConstraint("trading_account_id", "exchange_order_id", name="uq_exchange_order_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    exchange_order_id: Mapped[str] = mapped_column(String(128))
    client_order_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="", index=True)
    position_side: Mapped[str] = mapped_column(String(8), default="")
    order_side: Mapped[str] = mapped_column(String(8), default="")
    order_type: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    #: 枚举不统一（实测）：WEB / UNKNOWN_ORDER_SOURCE / STOP_LOSS / STOP_PROFIT。
    order_source: Mapped[str] = mapped_column(String(32), default="")
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    position_tpsl: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_fill_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_fill_value: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_fill_fee: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_realize_pnl: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_liquidate_fee: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    taker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    maker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    account_version: Mapped[int] = mapped_column(Integer, default=0)
    exchange_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exchange_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ExchangeAccountSample(Base):
    """WS ``account`` 频道的资金快照。

    净值曲线的数据源。``cum_deposit`` / ``cum_withdraw`` / ``cum_transfer_in``
    让第三方能剔除资金进出 —— 这是「净值增长不是靠充值做出来的」的唯一依据。
    """

    __tablename__ = "exchange_account_samples"
    __table_args__ = (
        UniqueConstraint(
            "trading_account_id", "account_version", "coin", name="uq_exchange_account_sample"
        ),
        Index("ix_exchange_account_samples_time", "trading_account_id", "captured_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    coin: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_deposit: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_withdraw: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_transfer_in: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_transfer_out: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_fill_fee: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    cum_funding_fee: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    liquidating: Mapped[bool] = mapped_column(Boolean, default=False)
    #: WS 的 v 字段。**会跳号**（实测 696→697→699→701），不是事件序列号，
    #: 只用作同一 (账户, 版本, 币种) 的幂等键。
    account_version: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

- [ ] **Step 4: 加迁移**

创建 `backend/alembic/versions/012_exchange_stream_events.py`：

```python
"""add exchange stream tables (WS fill / orders / account)

`001` 的 create_all 只在库处于 001 时执行过一次；已迁移的库不会再跑它，所以此后
新增的表不会自动出现。**只改 app/db/models.py 是不够的** —— 必须有一条
create_all(checkfirst=True) 的迁移把新表补出来，照 003 / 007 的写法。

create_all(checkfirst=True) 是幂等的，只会补建缺失的表。**列变更**仍然要用
002 那种显式写法，create_all 不会给已存在的表加列。
"""

from alembic import op

from app.db.models import Base

revision = "012_exchange_stream_events"
down_revision = "011_near_target_exit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # 只补建，不删表：无法判断哪些表是本迁移建的，删错会丢数据。
    pass
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/integration/test_migrations.py -q`

Expected: PASS

- [ ] **Step 6: 在开发库上跑一次迁移**

Run: `cd backend && uv run alembic upgrade head`

Expected: 无报错。若卡在 DDL 锁上，先停 worker —— 共享会话的
`state='idle in transaction'` 会阻塞 ALTER（见 `CLAUDE.md`）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/db/models.py backend/alembic/versions/012_exchange_stream_events.py backend/tests/integration/test_migrations.py
git commit -m "feat(db): 加采集层三张表（fill / order / account）

exchange_fills 是系统里第一份交易所回报的单笔盈亏。注意 realize_pnl 不含
手续费，列注释里写明了。

exchange_fills.symbol 可空：fill 推送里根本没有 symbol 字段（实测只有 coin
与 orderId），只能靠 orders 频道按 orderId 补。

exchange_account_samples 用 (账户, 版本, 币种) 做幂等键。account.v 会跳号
（实测 696→697→699→701），它不是事件序列号，只能当幂等键用。

迁移照 003/007 的 create_all(checkfirst=True) 写法 —— 只改 models.py 的话
已迁移的库不会建出这些表。"
```

---

## Task 6: 仓储层

**Files:**
- Create: `backend/app/streams/repository.py`
- Test: `backend/tests/unit/test_stream_repository.py`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_stream_repository.py`：

```python
"""事件落库。重点是幂等：WS 重连后服务端会重放，不能重复计数。"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import Base, ExchangeAccountSample, ExchangeFillRecord, ExchangeOrderEvent
from app.streams.protocol import AccountSample, FillEvent, OrderEvent
from app.streams.repository import StreamRepository


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'streams.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _fill(**overrides) -> FillEvent:
    values = {
        "exchange_fill_id": "f-1",
        "exchange_order_id": "o-1",
        "symbol": "",
        "position_side": "SHORT",
        "order_side": "BUY",
        "quantity": Decimal("100"),
        "value": Decimal("7.99"),
        "fee": Decimal("0.006392"),
        "realize_pnl": Decimal("0.01"),
        "direction": "TAKER",
        "filled_at": datetime(2026, 10, 15, 12, 58, 6, tzinfo=UTC),
        "account_version": 703,
        "raw": {"id": "f-1"},
    }
    values.update(overrides)
    return FillEvent(**values)


def _order(**overrides) -> OrderEvent:
    values = {
        "exchange_order_id": "o-1",
        "client_order_id": "4b73a61f-483f-457f-9a24-be0e9918cbe4",
        "symbol": "DOGESUSDT",
        "position_side": "SHORT",
        "order_side": "BUY",
        "order_type": "TAKE_PROFIT_MARKET",
        "status": "FILLED",
        "order_source": "STOP_PROFIT",
        "reduce_only": True,
        "position_tpsl": True,
        "trigger_price": Decimal("0.0799"),
        "quantity": Decimal("100"),
        "cum_fill_quantity": Decimal("100"),
        "cum_fill_value": Decimal("7.99"),
        "cum_fill_fee": Decimal("0.006392"),
        "cum_realize_pnl": Decimal("0.01"),
        "cum_liquidate_fee": Decimal("0"),
        "taker_fee_rate": Decimal("0.0008"),
        "maker_fee_rate": Decimal("0.0002"),
        "created_at": datetime(2026, 10, 15, 12, 58, 6, tzinfo=UTC),
        "updated_at": datetime(2026, 10, 15, 12, 58, 6, tzinfo=UTC),
        "account_version": 703,
        "raw": {"id": "o-1"},
    }
    values.update(overrides)
    return OrderEvent(**values)


def _sample(**overrides) -> AccountSample:
    values = {
        "coin": "SUSDT",
        "amount": Decimal("59010.73"),
        "cum_deposit": Decimal("0"),
        "cum_withdraw": Decimal("0"),
        "cum_transfer_in": Decimal("40000"),
        "cum_transfer_out": Decimal("0"),
        "cum_fill_fee": Decimal("0.006392"),
        "cum_funding_fee": Decimal("-56.35171893"),
        "liquidating": False,
        "captured_at": datetime(2026, 10, 15, 12, 58, 6, tzinfo=UTC),
        "account_version": 703,
        "raw": {"coin": "SUSDT"},
    }
    values.update(overrides)
    return AccountSample(**values)


def _repo(db: Session) -> StreamRepository:
    return StreamRepository(db, user_id="u-1", trading_account_id="acct-1")


def test_records_a_fill(db):
    repo = _repo(db)

    assert repo.record(_fill()) is True
    db.commit()

    row = db.scalar(select(ExchangeFillRecord))
    assert row.exchange_fill_id == "f-1"
    assert row.realize_pnl == Decimal("0.01")
    assert row.fee == Decimal("0.006392")


def test_recording_the_same_fill_twice_is_a_noop(db):
    """重连后服务端会重放，重复落库会让盈亏翻倍。"""

    repo = _repo(db)
    assert repo.record(_fill()) is True
    assert repo.record(_fill()) is False
    db.commit()

    assert len(db.scalars(select(ExchangeFillRecord)).all()) == 1


def test_order_event_upserts_in_place(db):
    """同一订单推多次（PENDING → FILLED），按 order_id 覆盖而不是追加。"""

    repo = _repo(db)
    repo.record(_order(status="PENDING", cum_realize_pnl=Decimal("0")))
    db.commit()
    repo.record(_order(status="FILLED"))
    db.commit()

    rows = db.scalars(select(ExchangeOrderEvent)).all()
    assert len(rows) == 1
    assert rows[0].status == "FILLED"
    assert rows[0].cum_realize_pnl == Decimal("0.01")


def test_fill_symbol_is_backfilled_from_the_order_event(db):
    """fill 推送里没有 symbol，靠 orders 频道按 orderId 补上。

    实测的到达顺序是 orders 先于 fill，但不能依赖它 —— 所以两个方向都要能补。
    """

    repo = _repo(db)

    # 顺序 A：order 先到，fill 后到 → fill 落库时就带上 symbol
    repo.record(_order())
    repo.record(_fill())
    db.commit()
    assert db.scalar(select(ExchangeFillRecord)).symbol == "DOGESUSDT"

    # 顺序 B：fill 先到，order 后到 → order 到达时回填
    repo.record(_fill(exchange_fill_id="f-2", exchange_order_id="o-2"))
    db.commit()
    assert db.scalar(
        select(ExchangeFillRecord).where(ExchangeFillRecord.exchange_fill_id == "f-2")
    ).symbol is None

    repo.record(_order(exchange_order_id="o-2"))
    db.commit()
    assert db.scalar(
        select(ExchangeFillRecord).where(ExchangeFillRecord.exchange_fill_id == "f-2")
    ).symbol == "DOGESUSDT"


def test_account_sample_is_idempotent_on_version(db):
    repo = _repo(db)
    assert repo.record(_sample()) is True
    assert repo.record(_sample()) is False
    db.commit()

    assert len(db.scalars(select(ExchangeAccountSample)).all()) == 1


def test_record_returns_false_for_unknown_event_types(db):
    repo = _repo(db)
    assert repo.record(object()) is False  # type: ignore[arg-type]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_stream_repository.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.streams.repository'`

- [ ] **Step 3: 写实现**

创建 `backend/app/streams/repository.py`：

```python
"""把 WS 事件落库。

全部按交易所 ID 幂等 —— 重连后服务端会重放同一批推送，重复落库会让盈亏翻倍。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExchangeAccountSample, ExchangeFillRecord, ExchangeOrderEvent
from app.streams.protocol import AccountSample, FillEvent, OrderEvent, StreamEvent

logger = logging.getLogger(__name__)


class StreamRepository:
    def __init__(self, db: Session, *, user_id: str, trading_account_id: str) -> None:
        self.db = db
        self.user_id = user_id
        self.trading_account_id = trading_account_id
        #: 本连接见过的 order_id → symbol。fill 推送里没有 symbol（实测），只能这样补。
        #: 实例级而不是模块级：order_id 是账户内的标识，两个账户的仓库不该互相看见；
        #: 监听器把一个仓库跨重连复用，所以连接断了缓存也还在。
        self._symbols: dict[str, str] = {}

    def record(self, event: StreamEvent | Any) -> bool:
        """落一条事件。返回是否新写入（``False`` = 已存在，跳过）。"""

        if isinstance(event, FillEvent):
            return self._record_fill(event)
        if isinstance(event, OrderEvent):
            return self._record_order(event)
        if isinstance(event, AccountSample):
            return self._record_account(event)
        logger.warning("ignoring unsupported stream event: %r", type(event).__name__)
        return False

    def record_all(self, events: list[StreamEvent]) -> int:
        """落一批事件，返回新写入的条数。调用方负责 ``commit()``。"""

        return sum(1 for event in events if self.record(event))

    def commit(self) -> None:
        self.db.commit()

    # --- 各频道的落库 ---

    def _record_fill(self, fill: FillEvent) -> bool:
        existing = self.db.scalar(
            select(ExchangeFillRecord).where(
                ExchangeFillRecord.trading_account_id == self.trading_account_id,
                ExchangeFillRecord.exchange_fill_id == fill.exchange_fill_id,
            )
        )
        if existing is not None:
            return False
        symbol = fill.symbol or self._symbols.get(fill.exchange_order_id) or None
        self.db.add(
            ExchangeFillRecord(
                id=str(uuid4()),
                user_id=self.user_id,
                trading_account_id=self.trading_account_id,
                exchange_fill_id=fill.exchange_fill_id,
                exchange_order_id=fill.exchange_order_id,
                symbol=symbol,
                position_side=fill.position_side,
                order_side=fill.order_side,
                quantity=fill.quantity,
                value=fill.value,
                fee=fill.fee,
                realize_pnl=fill.realize_pnl,
                direction=fill.direction,
                account_version=fill.account_version,
                filled_at=fill.filled_at,
                raw=fill.raw,
            )
        )
        return True

    def _record_order(self, event: OrderEvent) -> bool:
        if event.symbol:
            self._symbols[event.exchange_order_id] = event.symbol
        row = self.db.scalar(
            select(ExchangeOrderEvent).where(
                ExchangeOrderEvent.trading_account_id == self.trading_account_id,
                ExchangeOrderEvent.exchange_order_id == event.exchange_order_id,
            )
        )
        if row is None:
            row = ExchangeOrderEvent(
                id=str(uuid4()),
                user_id=self.user_id,
                trading_account_id=self.trading_account_id,
                exchange_order_id=event.exchange_order_id,
            )
            self.db.add(row)
        row.client_order_id = event.client_order_id
        row.symbol = event.symbol
        row.position_side = event.position_side
        row.order_side = event.order_side
        row.order_type = event.order_type
        row.status = event.status
        row.order_source = event.order_source
        row.reduce_only = event.reduce_only
        row.position_tpsl = event.position_tpsl
        row.trigger_price = event.trigger_price
        row.quantity = event.quantity
        row.cum_fill_quantity = event.cum_fill_quantity
        row.cum_fill_value = event.cum_fill_value
        row.cum_fill_fee = event.cum_fill_fee
        row.cum_realize_pnl = event.cum_realize_pnl
        row.cum_liquidate_fee = event.cum_liquidate_fee
        row.taker_fee_rate = event.taker_fee_rate
        row.maker_fee_rate = event.maker_fee_rate
        row.account_version = event.account_version
        row.exchange_created_at = event.created_at
        row.exchange_updated_at = event.updated_at
        row.raw = event.raw
        if event.symbol:
            self._backfill_fill_symbols(event.exchange_order_id, event.symbol)
        return True

    def _backfill_fill_symbols(self, exchange_order_id: str, symbol: str) -> None:
        """order 事件晚于 fill 到达时，把之前留空的 symbol 补上。"""

        for stale in self.db.scalars(
            select(ExchangeFillRecord).where(
                ExchangeFillRecord.trading_account_id == self.trading_account_id,
                ExchangeFillRecord.exchange_order_id == exchange_order_id,
                ExchangeFillRecord.symbol.is_(None),
            )
        ).all():
            stale.symbol = symbol

    def _record_account(self, sample: AccountSample) -> bool:
        existing = self.db.scalar(
            select(ExchangeAccountSample).where(
                ExchangeAccountSample.trading_account_id == self.trading_account_id,
                ExchangeAccountSample.account_version == sample.account_version,
                ExchangeAccountSample.coin == sample.coin,
            )
        )
        if existing is not None:
            return False
        self.db.add(
            ExchangeAccountSample(
                id=str(uuid4()),
                user_id=self.user_id,
                trading_account_id=self.trading_account_id,
                coin=sample.coin,
                amount=sample.amount,
                cum_deposit=sample.cum_deposit,
                cum_withdraw=sample.cum_withdraw,
                cum_transfer_in=sample.cum_transfer_in,
                cum_transfer_out=sample.cum_transfer_out,
                cum_fill_fee=sample.cum_fill_fee,
                cum_funding_fee=sample.cum_funding_fee,
                liquidating=sample.liquidating,
                account_version=sample.account_version,
                captured_at=sample.captured_at,
                raw=sample.raw,
            )
        )
        return True
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_stream_repository.py -q`

Expected: PASS（7 passed）

- [ ] **Step 5: 静态检查**

Run: `cd backend && uv run ruff check app/streams`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add backend/app/streams/repository.py backend/tests/unit/test_stream_repository.py
git commit -m "feat(streams): 事件落库，按交易所 ID 幂等

重连后服务端会重放同一批推送，不幂等会让盈亏翻倍 —— 所以三张表各有唯一键，
落库前先查。

orders 事件是覆盖而不是追加：同一订单推多次（UNTRIGGERED → PENDING →
FILLED），终态那一行已经带齐累计字段，留中间态没有额外信息。

fill 推送没有 symbol（实测只有 coin 与 orderId），靠 orders 频道按 orderId
补。两个到达顺序都要能补：order 先到时直接带上，fill 先到时由 order 到达
那一刻回填。"
```

---

## Task 7: WebSocket 客户端

**Files:**
- Create: `backend/app/streams/client.py`
- Test: `backend/tests/unit/test_stream_client.py`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_stream_client.py`：

```python
"""WS 客户端。IO 只有 Transport 那一层，其余全用假对象测。

项目里没有 pytest-asyncio，所以用 asyncio.run 包一个 sync 测试函数。
"""

import asyncio
import json

import pytest

from app.streams.client import PrivateStreamClient
from app.streams.protocol import FillEvent

FILL_ROW = {
    "id": "f-1",
    "coin": "SUSDT",
    "orderId": "o-1",
    "positionSide": "SHORT",
    "orderSide": "BUY",
    "fillSize": "100",
    "fillValue": "7.99",
    "fillFee": "0.006392",
    "realizePnl": "0.01",
    "direction": "TAKER",
    "createdTime": "1792069086664",
    "updatedTime": "1792069086664",
}


class FakeTransport:
    def __init__(self, script):
        self._script = list(script)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._script:
            raise ConnectionError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def _client(transport, **overrides):
    async def connect(*, url, headers):
        connect.headers = headers
        return transport

    connect.headers = {}
    values = {
        "url": "wss://example.test/v3/ws/private",
        "api_key": "key-1",
        "api_secret": "secret-1",
        "passphrase": "pass-1",
        "channels": ("fill",),
        "connect": connect,
        "clock_ms": lambda: 1700000000000,
    }
    values.update(overrides)
    return PrivateStreamClient(**values), connect


def test_run_once_subscribes_answers_ping_and_dispatches_events():
    transport = FakeTransport(
        [
            json.dumps({"event": "connected", "cid": "x"}),
            json.dumps({"result": True, "id": 1}),
            json.dumps({"type": "ping", "time": "1"}),
            json.dumps({"e": "fill", "E": 1, "v": 703, "d": [FILL_ROW]}),
            ConnectionError("server closed"),
        ]
    )
    received: list = []
    client, connect = _client(transport)

    async def scenario():
        with pytest.raises(ConnectionError):
            await client.run_once(received.extend)

    asyncio.run(scenario())

    sent = [json.loads(item) for item in transport.sent]
    assert sent[0] == {"method": "SUBSCRIBE", "params": ["fill"], "id": 1}
    assert sent[1] == {"method": "PONG", "id": 1}
    assert transport.closed is True
    assert len(received) == 1
    assert isinstance(received[0], FillEvent)


def test_run_once_passes_signed_headers_to_the_transport():
    transport = FakeTransport([ConnectionError("boom")])
    client, connect = _client(transport)

    async def scenario():
        with pytest.raises(ConnectionError):
            await client.run_once(lambda events: None)

    asyncio.run(scenario())

    assert connect.headers["ACCESS-KEY"] == "key-1"
    assert connect.headers["ACCESS-TIMESTAMP"] == "1700000000000"
    assert connect.headers["ACCESS-SIGN"]


def test_run_once_closes_the_transport_even_when_the_loop_fails():
    transport = FakeTransport([ValueError("bad frame")])
    client, _ = _client(transport)

    async def scenario():
        with pytest.raises(ValueError):
            await client.run_once(lambda events: None)

    asyncio.run(scenario())

    assert transport.closed is True


def test_run_forever_backs_off_exponentially_and_can_stop():
    delays: list[float] = []
    attempts = {"count": 0}

    async def connect(*, url, headers):
        attempts["count"] += 1
        raise ConnectionError("unreachable")

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    client = PrivateStreamClient(
        url="wss://example.test/v3/ws/private",
        api_key="k",
        api_secret="s",
        passphrase="p",
        channels=("fill",),
        connect=connect,
        clock_ms=lambda: 1700000000000,
        sleep=sleep,
        initial_backoff=1.0,
        max_backoff=4.0,
    )

    asyncio.run(client.run_forever(lambda events: None, should_stop=lambda: attempts["count"] >= 4))

    assert delays == [1.0, 2.0, 4.0]


def test_run_forever_does_not_back_off_after_a_clean_return():
    """正常返回（例如收到停止信号）不算故障，退避要重置。"""

    delays: list[float] = []
    rounds = {"count": 0}

    async def connect(*, url, headers):
        return FakeTransport([])  # recv 立刻抛 ConnectionError

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        rounds["count"] += 1

    client = PrivateStreamClient(
        url="wss://example.test/v3/ws/private",
        api_key="k",
        api_secret="s",
        passphrase="p",
        channels=("fill",),
        connect=connect,
        clock_ms=lambda: 1700000000000,
        sleep=sleep,
        initial_backoff=1.0,
        max_backoff=4.0,
    )

    asyncio.run(client.run_forever(lambda events: None, should_stop=lambda: rounds["count"] >= 3))

    # 每轮都是 ConnectionError，所以仍然会翻倍 —— 这条只锁住上限不被越过。
    assert delays == [1.0, 2.0, 4.0]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_stream_client.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.streams.client'`

- [ ] **Step 3: 写实现**

创建 `backend/app/streams/client.py`：

```python
"""WEEX 私有 WebSocket 客户端。

设计上把 IO 压到最小：所有协议逻辑在 protocol.py（纯函数），这里只负责
「连上 → 订阅 → 转发 → 断了退避重连」。socket 通过 ``connect`` 注入，
于是测试可以塞一个假 Transport，不需要起服务器。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import websockets

from app.streams.protocol import (
    ACK,
    CONNECTED,
    EVENT,
    PING,
    SUBSCRIBE_CHANNELS,
    StreamEvent,
    classify,
    parse_event,
    pong_message,
    subscribe_message,
    ws_auth_headers,
)

logger = logging.getLogger(__name__)

USER_AGENT = "alpha-council-streams/1.0"

#: 建连超时。超过就当成一次失败连接，走退避重试。
OPEN_TIMEOUT_SECONDS = 20.0


class Transport(Protocol):
    """socket 的最小接口。假对象实现这三个方法即可替掉网络。"""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class _WebsocketTransport:
    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send(self, message: str) -> None:
        await self._socket.send(message)

    async def recv(self) -> str:
        return await self._socket.recv()

    async def close(self) -> None:
        await self._socket.close()


async def default_connect(*, url: str, headers: dict[str, str]) -> Transport:
    socket = await websockets.connect(
        url,
        additional_headers=headers,
        user_agent_header=USER_AGENT,
        # WEEX 用自己的应用层 JSON ping。关掉协议层自动 ping，免得两套心跳互相干扰。
        ping_interval=None,
        open_timeout=OPEN_TIMEOUT_SECONDS,
    )
    return _WebsocketTransport(socket)


class PrivateStreamClient:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
        channels: tuple[str, ...] = SUBSCRIBE_CHANNELS,
        connect: Callable[..., Awaitable[Transport]] = default_connect,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.channels = channels
        self.connect = connect
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff

    async def run_once(self, on_events: Callable[[list[StreamEvent]], None]) -> None:
        """连一次、订阅、转发，直到连接断开。

        断开通过异常向上抛（``ConnectionError`` / ``websockets.ConnectionClosed``），
        由 ``run_forever`` 决定怎么退避。
        """

        headers = ws_auth_headers(
            api_key=self.api_key,
            api_secret=self.api_secret,
            passphrase=self.passphrase,
            timestamp_ms=self.clock_ms(),
        )
        transport = await self.connect(url=self.url, headers=headers)
        try:
            for index, channel in enumerate(self.channels, start=1):
                await transport.send(subscribe_message(channel, index))
            logger.info("private stream subscribed: %s", list(self.channels))
            while True:
                incoming = classify(await transport.recv())
                if incoming.kind == PING:
                    await transport.send(pong_message())
                elif incoming.kind == EVENT:
                    events = parse_event(incoming.payload)
                    if events:
                        on_events(events)
                elif incoming.kind == ACK and not incoming.payload.get("result"):
                    logger.warning("private stream subscription rejected: %s", incoming.payload)
                elif incoming.kind == CONNECTED:
                    logger.info("private stream connected: %s", incoming.payload.get("cid"))
        finally:
            await transport.close()

    async def run_forever(
        self,
        on_events: Callable[[list[StreamEvent]], None],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """断线重连的主循环。指数退避，成功建连后退避重置。"""

        backoff = self.initial_backoff
        while should_stop is None or not should_stop():
            try:
                await self.run_once(on_events)
                backoff = self.initial_backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何连接故障都要退避重试，不能终止进程
                logger.warning(
                    "private stream disconnected (%s: %s); retrying in %.1fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await self.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_stream_client.py -q`

Expected: PASS（5 passed）

- [ ] **Step 5: 静态检查**

Run: `cd backend && ruff check app/streams`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add backend/app/streams/client.py backend/tests/unit/test_stream_client.py
git commit -m "feat(streams): WS 客户端（订阅 / PONG / 指数退避重连）

socket 通过 connect 参数注入，测试塞假 Transport 就能覆盖订阅、心跳、事件
转发与异常路径，不用起服务器 —— 项目里没有 pytest-asyncio，用 asyncio.run
包 sync 测试函数即可。

关掉 websockets 的协议层自动 ping：WEEX 用自己的应用层 JSON ping，两套心跳
并行会互相干扰。"
```

---

## Task 8: 监听进程与进程入口

**Files:**
- Create: `backend/app/streams/listener.py`
- Create: `backend/workers/streams.py`
- Test: `backend/tests/unit/test_stream_listener.py`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_stream_listener.py`：

```python
"""监听器的账户扫描。

只测纯逻辑那部分：常驻循环本身由 probe_private_ws.py 在真实账户上验证过，
单测再起一遍 socket 没有增量价值。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, TradingAccount, User
from app.streams.listener import load_targets


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'listener.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="u-1", clerk_user_id="clerk-u1"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _account(db: Session, **overrides) -> TradingAccount:
    values = {
        "id": "acct-1",
        "user_id": "u-1",
        "provider": "weex",
        "environment": "virtual",
        "api_key_ref": "key-1",
        "api_secret_ref": "secret-1",
        "passphrase_ref": "pass-1",
        "enabled": True,
    }
    values.update(overrides)
    account = TradingAccount(**values)
    db.add(account)
    db.commit()
    return account


def test_loads_enabled_virtual_accounts_with_credentials(db):
    _account(db)

    targets = load_targets(db)

    assert len(targets) == 1
    assert targets[0].user_id == "u-1"
    assert targets[0].trading_account_id == "acct-1"
    assert targets[0].credentials.api_key == "key-1"


def test_skips_disabled_accounts(db):
    _account(db, enabled=False)

    assert load_targets(db) == []


def test_skips_accounts_without_credentials(db):
    """没配凭证的账户跳过即可 —— 不是错误，但要能被日志看见。"""

    _account(db, passphrase_ref=None)

    assert load_targets(db) == []


def test_skips_non_virtual_environments(db):
    """只跟虚拟盘。正式盘不该被这个采集层碰到。"""

    _account(db, environment="live")

    assert load_targets(db) == []


def test_build_client_subscribes_the_three_channels():
    from app.streams.client import PrivateStreamClient
    from app.streams.listener import build_client

    client = build_client(
        url="wss://example.test/v3/ws/private",
        api_key="k",
        api_secret="s",
        passphrase="p",
        initial_backoff=1.0,
        max_backoff=8.0,
    )

    assert isinstance(client, PrivateStreamClient)
    assert client.channels == ("fill", "orders", "account")
    assert client.max_backoff == 8.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_stream_listener.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.streams.listener'`

- [ ] **Step 3: 写实现**

创建 `backend/app/streams/listener.py`：

```python
"""常驻监听进程：每个启用的虚拟账户一条私有 WS 连接。

**与交易路径完全解耦**（docs/web3-roadmap.md 的原则）：这个进程崩了不影响下单，
重启后靠 REST 对账补漏。所以它单独跑，不塞进 workers/scheduler.py。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import TradingAccount
from app.db.session import SessionLocal
from app.exchange.credentials import credentials_for_account
from app.exchange.weex import WeexCredentials
from app.streams.client import PrivateStreamClient
from app.streams.protocol import SUBSCRIBE_CHANNELS, StreamEvent
from app.streams.repository import StreamRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamTarget:
    user_id: str
    trading_account_id: str
    credentials: WeexCredentials


def load_targets(db: Session) -> list[StreamTarget]:
    """启用中、有凭证的虚拟账户。

    条件与 ``TradingCycleService.enabled_user_ids`` 保持一致（provider=weex、
    environment=virtual、enabled），但按账户返回而不是按用户 —— 采集需要账户 id
    做落库的归属键。
    """

    accounts = db.scalars(
        select(TradingAccount).where(
            TradingAccount.enabled.is_(True),
            TradingAccount.provider == "weex",
            TradingAccount.environment == "virtual",
        )
    ).all()
    targets: list[StreamTarget] = []
    for account in accounts:
        credentials = credentials_for_account(account)
        if credentials is None:
            logger.warning("stream target skipped: account %s has no credentials", account.id)
            continue
        targets.append(
            StreamTarget(
                user_id=account.user_id,
                trading_account_id=account.id,
                credentials=credentials,
            )
        )
    return targets


def build_client(
    *,
    url: str,
    api_key: str,
    api_secret: str,
    passphrase: str,
    initial_backoff: float,
    max_backoff: float,
) -> PrivateStreamClient:
    return PrivateStreamClient(
        url=url,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        channels=SUBSCRIBE_CHANNELS,
        initial_backoff=initial_backoff,
        max_backoff=max_backoff,
    )


class StreamListener:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def run_forever(self) -> None:
        """扫描账户 → 每个账户一条连接。

        没有可用账户时**不退出** —— 退出会被 compose 的 restart 策略拉起来，
        变成崩溃循环，日志里只剩重启噪音。改为定期重扫。
        """

        while True:
            with self.session_factory() as db:
                targets = load_targets(db)
            if not targets:
                logger.warning(
                    "no enabled virtual accounts with credentials; re-checking in %.0fs",
                    self.settings.streams_rescan_seconds,
                )
                await asyncio.sleep(self.settings.streams_rescan_seconds)
                continue
            logger.info("starting %d private stream(s)", len(targets))
            await asyncio.gather(*(self._run_target(target) for target in targets))
            await asyncio.sleep(self.settings.streams_rescan_seconds)

    async def _run_target(self, target: StreamTarget) -> None:
        """一个账户一条连接。每个账户独占一个 Session —— 共享会话在并发写时
        会互相踩事务（worker 那边靠串行规避，这里用 gather 就不行了）。"""

        client = build_client(
            url=self.settings.streams_ws_url,
            api_key=target.credentials.api_key,
            api_secret=target.credentials.api_secret,
            passphrase=target.credentials.passphrase,
            initial_backoff=self.settings.streams_reconnect_initial_seconds,
            max_backoff=self.settings.streams_reconnect_max_seconds,
        )
        with self.session_factory() as db:
            repository = StreamRepository(
                db, user_id=target.user_id, trading_account_id=target.trading_account_id
            )
            await client.run_forever(lambda events: self._persist(repository, events))

    @staticmethod
    def _persist(repository: StreamRepository, events: list[StreamEvent]) -> None:
        """落一批事件。

        失败必须 rollback —— 共享会话一旦处于待回滚状态，后续每一次 flush 都会
        抛 PendingRollbackError，整条连接就废了（worker 那边踩过同一个坑）。
        """

        try:
            written = repository.record_all(events)
            repository.commit()
        except Exception:  # noqa: BLE001 - 落库失败不能中断连接
            logger.exception("failed to persist %d stream event(s)", len(events))
            repository.db.rollback()
            return
        logger.debug("persisted %d/%d stream event(s)", written, len(events))
```

创建 `backend/workers/streams.py`：

```python
"""私有 WS 采集进程入口。

与 workers/scheduler.py 分开跑：采集是长连接，交易是 5 分钟一轮的短事务，
混在一个进程里会让 worker 的失败面扩大到网络长连接。
"""

import asyncio
import logging

from app.config import get_settings
from app.logging import configure_logging
from app.streams.listener import StreamListener

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.streams_enabled:
        logger.info("private stream listener disabled (STREAMS_ENABLED=false)")
        return
    logger.info("private stream listener starting: url=%s", settings.streams_ws_url)
    asyncio.run(StreamListener(settings=settings).run_forever())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_stream_listener.py -q`

Expected: PASS（5 passed）

- [ ] **Step 5: 跑全部单元测试确认没有回归**

Run: `cd backend && uv run pytest tests/unit -q`

Expected: 全部通过

- [ ] **Step 6: 静态检查**

Run: `cd backend && uv run ruff check app tests workers && uv run mypy app`

Expected: ruff `All checks passed!`；mypy 无新增错误

- [ ] **Step 7: 对着真实账户跑一次**

Run: `cd backend && uv run python -m workers.streams`

Expected: 日志出现 `private stream subscribed: ['fill', 'orders', 'account']`。
然后在 WEEX 网页手动开一笔仓再平掉，观察日志里出现 `persisted 1/N stream event(s)`。
用 `Ctrl-C` 结束。

落库核对（把 `<账户 id>` 换成 `trading_accounts.id`）：

```bash
cd backend && uv run python -c "
from sqlalchemy import select, func
from app.db.session import SessionLocal
from app.db.models import ExchangeFillRecord, ExchangeOrderEvent, ExchangeAccountSample
with SessionLocal() as db:
    print('fills', db.scalar(select(func.count()).select_from(ExchangeFillRecord)))
    print('orders', db.scalar(select(func.count()).select_from(ExchangeOrderEvent)))
    print('samples', db.scalar(select(func.count()).select_from(ExchangeAccountSample)))
    for row in db.scalars(select(ExchangeFillRecord).order_by(ExchangeFillRecord.filled_at.desc()).limit(3)):
        print(row.symbol, row.order_side, row.quantity, 'realize', row.realize_pnl, 'fee', row.fee)
"
```

Expected: fills 至少 2 条（开 + 平），且**平仓那条的 `realize_pnl` 与
`realize_pnl - fee` 都不是 0**。`symbol` 不为 None（由 orders 事件补上）。

- [ ] **Step 8: 提交**

```bash
git add backend/app/streams/listener.py backend/workers/streams.py backend/tests/unit/test_stream_listener.py
git commit -m "feat(streams): 常驻监听进程与入口

每账户一条连接、一个 Session —— gather 并发时共享会话会互相踩事务，
worker 那边靠串行规避，这里不能照搬。

落库失败必须 rollback：共享会话一旦处于待回滚状态，后续每次 flush 都抛
PendingRollbackError，整条连接就废了。

没有可用账户时不退出，改为定期重扫 —— 退出会被 compose 的 restart 拉起来
变成崩溃循环。"
```

---

## Task 9: 部署编排

**Files:**
- Modify: `docker-compose.yml`（在 `worker` 服务之后）
- Modify: `docs/runbook.md`（追加一节）

- [ ] **Step 1: 加服务**

在 `docker-compose.yml` 的 `worker` 服务之后插入：

```yaml
  streams:
    build: ./backend
    restart: unless-stopped
    env_file:
      - ./backend/.env
    # 采集层与交易路径完全解耦：它崩了不影响下单，重启后靠 REST 对账补漏。
    # 刻意与 worker 分开 —— 长连接和 5 分钟一轮的短事务混在一个进程里，
    # 会让 worker 的失败面扩大到网络。
    command: python -m workers.streams
    # 只连数据库与交易所，不对外暴露端口
```

- [ ] **Step 2: 验证 compose 语法**

Run: `docker compose config >/dev/null && echo OK`

Expected: `OK`

- [ ] **Step 3: 在 runbook 里记一节**

在 `docs/runbook.md` 末尾追加：

```markdown
## 私有 WS 采集层（streams）

`docker compose logs -f streams` 是排查它的第一入口。

| 现象 | 原因 | 处理 |
|---|---|---|
| 日志刷 `private stream disconnected ... retrying in Xs` | 网络或端点不可达 | 退避上限 60s，通常会自愈；持续超过 10 分钟查 `WEEX_BASE_URL` 所在网络 |
| 日志刷 `no enabled virtual accounts with credentials` | 没有启用中的虚拟账户，或凭证没配 | 检查 `trading_accounts` 的 `enabled` / `api_key_ref` 等三列 |
| 有 `subscribed` 但成交后没有 `persisted` | 频道没推，或推送被跳过 | 用 `backend/scripts/probe_private_ws.py` 单独连一次对比 |
| 落库表里 `realize_pnl` 与预期不符 | 该字段**不含手续费** | 净额是 `realize_pnl − fee`，不是 `realize_pnl` |

**注意**：这个进程停摆期间的成交不会自动补齐 —— WS 不重放历史。补漏要另跑
`order/history` 对账（见 spec §12.2），而那个接口只有 100 条窗口且不能翻页。
```

- [ ] **Step 4: 提交**

```bash
git add docker-compose.yml docs/runbook.md
git commit -m "chore(deploy): 加 streams 服务并写排障手册

单独一个服务而不是塞进 worker：长连接和 5 分钟一轮的短事务混在一起，
会让 worker 的失败面扩大到网络。

runbook 里点明两件容易误判的事：realize_pnl 不含手续费；进程停摆期间的
成交不会自动补齐（WS 不重放历史）。"
```

---

## 完成标准

- [ ] `uv run pytest` 全绿
- [ ] `uv run ruff check app tests workers` 通过
- [ ] 对着真实账户跑 `python -m workers.streams`，手动开平一笔仓后
      `exchange_fills` 里有两条记录，平仓那条的 `realize_pnl` 非 0
- [ ] `docker compose config` 通过

## 不在本计划范围内

- **链上存证**（Merkle / IPFS / 合约 / subgraph）—— 等这一层积累出真实数据后单独计划
- **REST `order/history` 兜底对账与 `observation_gap` 检测** —— 依赖本层先跑起来
- **`positions` 频道与合约 ID 映射** —— 它给的累计费用在 `account` 频道里有聚合版本
- **把 `exchange_fills` 接进 `ReconciliationService`** —— 会改变风控的日亏损与连亏
  熔断输入，需要单独评估（现在用的是 Δbalance，换源会改变熔断行为）


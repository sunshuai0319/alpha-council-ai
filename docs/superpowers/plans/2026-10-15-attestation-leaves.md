# 链上存证叶子生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把采集层已经落库的交易所数据，转成「待上链叶子」的明文 payload 与叶子哈希。

**Architecture:** 两层，都保持纯函数以方便测试。`payload.py` 负责规范化序列化与哈希 —— 叶子哈希必须逐字节确定，否则链上锚定的东西对不上。`leaves.py` 负责把成交腿配对成往返、组装 payload，它只吃普通 dataclass，不碰 SQLAlchemy；另有一个薄查询层把 ORM 行映射成那些 dataclass。

**Tech Stack:** Python 3.13、`eth_utils.keccak`（`eth-account` 已带）、SQLAlchemy 2.x、pytest。

---

## 背景

上游 spec：`docs/superpowers/specs/2026-10-15-onchain-trade-attestation-design.md`
（下称 spec）。本计划实现它的 **§16 接口契约**，产出 spec §4 里 ②③④⑤ 中的 **② 结果叶子**与
**⑤ 净值叶子**。

**本计划的边界**：只生成 payload 与哈希。**不碰 IPFS、不碰加密、不碰链**。做完之后
`(payload_plaintext, leaf_hash)` 是一等产物，下一份计划才把它加密、pin、写进 Merkle 树。

### 上游已经就绪的东西

采集层已在 `main`（`app/streams/`），三张表已有真实数据：

| 表 | 关键列 |
|---|---|
| `exchange_fills` | `exchange_fill_id` / `exchange_order_id` / `symbol` / `position_side` / `order_side` / `quantity` / `value` / `fee` / `realize_pnl` / `filled_at` |
| `exchange_order_events` | `exchange_order_id` / `client_order_id` / `order_source` / `reduce_only` / `trigger_price` / `cum_liquidate_fee` |
| `exchange_account_samples` | `amount` / `cum_deposit` / `cum_withdraw` / `cum_transfer_in` / `cum_fill_fee` / `cum_funding_fee` / `account_version` / `captured_at` |

**库里现有一次真实往返**（本计划的验收样本）：

```
开仓 DOGE-USDT LONG BUY  100  value=7.917  realize=0        fee=0.0063336
平仓 DOGE-USDT LONG SELL 100  value=7.918  realize=0.001    fee=0.0063344
```

期望的叶子数值：`fee=0.012668`、`pnl_net=-0.011668`。

### 五条来自实测的硬约束

1. **开仓腿的 `realizePnl` 恒为 0**，盈亏全在平仓腿。所以 `realize_pnl` 取**平仓腿**，
   而 `fee` 是**两腿之和** —— 单条 fill 的 `realizePnl − fee` 只是半程。
2. **`fill` 推送里没有 `symbol`**，靠 REST 对账回填。生成叶子前它可能还是 NULL，
   见 §16.3。
3. **`reduce_only` 只在 `exchange_order_events` 里**，`fill` 没有。所以必须 join，
   否则分不出开仓腿与平仓腿。
4. **表里没有「均价」列**，只有成交额与数量 —— `avg = value / quantity`。
5. **净值变化可以剔除资金进出**（`cum_deposit` / `cum_withdraw` / `cum_transfer_in`），
   这是 P1 最硬的证据，别漏掉这几个字段。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/app/web3/__init__.py` | 包标记（实现后再创建；当前目录已清理，合约位于根目录 `contracts/`） |
| `backend/app/web3/payload.py` | 规范化 JSON + 叶子哈希 + schema 常量。**纯函数** |
| `backend/app/web3/leaves.py` | 成交腿配对、结果叶子与净值叶子的组装、查询层 |
| `backend/scripts/dump_attestation_leaves.py` | 对着真实库打印叶子，人工核对 |
| `backend/tests/unit/test_web3_payload.py` | 规范化与哈希的确定性 |
| `backend/tests/unit/test_web3_leaves.py` | 配对与组装 |

`payload.py` 与 `leaves.py` 的组装函数都不碰网络、不碰 DB；`leaves.py` 的查询层是唯一
碰 SQLAlchemy 的部分，单独测。

---

## Task 1: 规范化序列化与叶子哈希

叶子哈希定义是 `keccak256(明文 payload 的规范化 JSON)`（spec §4.1）。规范化必须逐字节
确定 —— 换一种 JSON 写法就换一个哈希，链上锚定的东西全对不上。

**Files:**
- Create: `backend/app/web3/__init__.py`
- Create: `backend/app/web3/payload.py`
- Test: `backend/tests/unit/test_web3_payload.py`

- [ ] **Step 1: 建包**

创建 `backend/app/web3/__init__.py`，内容为空（与 `app/positions/__init__.py` 一致）。

- [ ] **Step 2: 写失败的测试**

创建 `backend/tests/unit/test_web3_payload.py`：

```python
"""叶子 payload 的规范化序列化与哈希。

**这个模块的确定性就是链上锚定的可信度。** 换一种 JSON 写法就换一个哈希，
所以每条规范化规则都要有测试锁住。
"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from app.web3.payload import canonical_json, leaf_hash


def test_keys_are_sorted():
    """键顺序不影响哈希 —— 否则构造 payload 时的字典顺序会改变结果。"""

    first = canonical_json({"b": 1, "a": 2})
    second = canonical_json({"a": 2, "b": 1})

    assert first == second == '{"a":2,"b":1}'


def test_decimal_becomes_a_string():
    """JSON 数字是浮点，0.1 + 0.2 那种精度问题会把哈希搞得不稳定。

    实测的成交额是 7.91700 这种定点字符串，必须原样保住。
    """

    assert canonical_json({"fee": Decimal("0.00633360")}) == '{"fee":"0.00633360"}'


def test_datetime_becomes_epoch_milliseconds():
    moment = datetime(2026, 10, 15, 13, 23, 28, 667000, tzinfo=UTC)

    assert canonical_json({"at": moment}) == '{"at":1792070608667}'


def test_datetime_is_interpreted_as_utc_when_naive():
    """SQLite 取回来是 naive 的（Postgres 是 aware）。

    如果不按 UTC 解释，同一条记录在不同时区的机器上会算出**不同的哈希** ——
    链上锚定的东西就废了。见 app/positions/manager.py 同样的处理。
    """

    naive = datetime(2026, 10, 15, 13, 23, 28, 667000)
    aware = datetime(2026, 10, 15, 13, 23, 28, 667000, tzinfo=UTC)

    assert canonical_json({"at": naive}) == canonical_json({"at": aware})


def test_datetime_is_normalised_across_timezones():
    """同一时刻的不同时区表示，哈希必须相同。"""

    utc = datetime(2026, 10, 15, 13, 0, 0, tzinfo=UTC)
    beijing = utc.astimezone(timezone(timedelta(hours=8)))

    assert canonical_json({"at": utc}) == canonical_json({"at": beijing})


def test_no_whitespace_and_no_ascii_escaping():
    assert canonical_json({"a": [1, 2]}) == '{"a":[1,2]}'
    assert canonical_json({"kind": "结果"}) == '{"kind":"结果"}'


def test_nested_structures_are_normalised():
    payload = {"outer": {"inner": [Decimal("1.50"), {"deep": Decimal("2")}]}}

    assert canonical_json(payload) == '{"outer":{"inner":["1.50",{"deep":"2"}]}}'


def test_none_and_bool_pass_through():
    assert canonical_json({"a": None, "b": True}) == '{"a":null,"b":true}'


def test_leaf_hash_is_keccak256_of_the_canonical_bytes():
    """链上锚定的就是这个值，所以它必须是 keccak256，不是 sha256。"""

    from eth_utils import keccak

    payload = {"kind": "result", "quantity": Decimal("100")}
    expected = "0x" + keccak(canonical_json(payload).encode("utf-8")).hex()

    assert leaf_hash(payload) == expected


def test_leaf_hash_is_stable_regardless_of_key_order():
    assert leaf_hash({"b": 1, "a": Decimal("1")}) == leaf_hash({"a": Decimal("1"), "b": 1})


def test_leaf_hash_is_0x_prefixed_and_32_bytes():
    digest = leaf_hash({"kind": "equity"})

    assert digest.startswith("0x")
    assert len(digest) == 66
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_web3_payload.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.web3.payload'`

- [ ] **Step 4: 写实现**

创建 `backend/app/web3/payload.py`：

```python
"""叶子 payload 的规范化序列化与哈希。

**这一层是链上锚定的根基。** 叶子哈希定义为 ``keccak256(明文 payload 的规范化
JSON)``（spec §4.1），所以「规范化」必须逐字节确定 —— 换一种 JSON 写法就换一个
哈希，链上锚定的东西全部对不上。改动这里的任何一条规则都等于让已上链的记录
验不过，别随手改。

四条规则：

1. **键排序** —— 否则构造 payload 时字典的插入顺序会改变哈希
2. **Decimal → 字符串** —— JSON 数字是浮点，定点金额会被精度问题搞得不稳定
3. **datetime → epoch 毫秒整数** —— 跨时区无歧义，且与交易所、数据库的源一致
4. **无空白分隔符、不转义非 ASCII** —— 只要能少一个变量就少一个

**哈希与加密无关**：加密只影响 IPFS 上的存储形式，明文规范化后哈希不变。
所以「运营方托管密钥」升级到「用户钱包加密」不需要任何链上改动（spec §4.1）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from eth_utils import keccak

#: payload 的 schema 标识。**改动 payload 的字段集就要改它** ——
#: 第三方靠它决定怎么解析，静默加字段会让老解析器算错的账。
SCHEMA = "alpha-council/attestation-v1"


def _epoch_millis(value: datetime) -> int:
    """epoch 毫秒。

    naive 的按 UTC 解释。SQLite 取回来是 naive 的（Postgres 是 aware）——
    不统一的话同一条记录在不同时区的机器上会算出**不同的哈希**，
    链上锚定的东西就废了。见 app/positions/manager.py 同样的处理。
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _normalise(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _epoch_millis(value)
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def canonical_json(payload: dict[str, Any]) -> str:
    """规范化 JSON 文本。同一条 payload 必须永远得到同一个字符串。"""

    return json.dumps(
        _normalise(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def leaf_hash(payload: dict[str, Any]) -> str:
    """叶子哈希，``0x`` 前缀的 keccak256。链上锚定的就是这个值。"""

    return "0x" + keccak(canonical_json(payload).encode("utf-8")).hex()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_web3_payload.py -q`

Expected: PASS（11 passed）

- [ ] **Step 6: 静态检查**

Run: `cd backend && uv run ruff check app/web3 tests/unit/test_web3_payload.py`

Expected: `All checks passed!`

- [ ] **Step 7: 提交**

```bash
git add backend/app/web3/__init__.py backend/app/web3/payload.py backend/tests/unit/test_web3_payload.py
git commit -m "feat(web3): 叶子 payload 的规范化序列化与哈希

叶子哈希 = keccak256(明文 payload 的规范化 JSON)，所以规范化规则必须逐字节
确定 —— 换一种写法就换一个哈希，链上锚定的东西全对不上。四条规则都有测试
锁住：键排序、Decimal 转字符串（JSON 数字是浮点，定点金额会不稳定）、
datetime 转 epoch 毫秒、无空白不转义。

naive datetime 按 UTC 解释这条单独立了个测试：SQLite 取回来是 naive 的，
不统一的话同一条记录在不同时区的机器上哈希不同，链上锚定就废了。

payload.py 不碰加密 —— 哈希基于明文，加密只影响存储形式，所以日后从
「运营方托管密钥」升级到「用户钱包加密」不需要任何链上改动。"
```

---

## Task 2: 成交腿配对

一笔往返 = 开仓腿 + 平仓腿。**交易所已经在平仓腿上给了 `realizePnl`，所以配对不是为了
算盈亏** —— 是为了把两腿归成一条记录，并拿到往返的**总手续费**与**数量**。

配对按 `(symbol, position_side)` 分组的 FIFO：一个平仓腿按时间顺序消费开仓腿，
一条开仓腿可以被拆给多次平仓（部分平仓）。

**Files:**
- Create: `backend/app/web3/leaves.py`
- Test: `backend/tests/unit/test_web3_leaves.py`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_web3_leaves.py`：

```python
"""成交腿配对、结果叶子与净值叶子的组装。

这些函数都是纯的：吃 dataclass，吐 dataclass / dict。查询层（Task 4）单独测。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.web3.leaves import FillLeg, match_round_trips

BASE = datetime(2026, 10, 15, 13, 23, 28, 667000, tzinfo=UTC)


def _leg(
    fill_id: str,
    *,
    symbol: str = "DOGE-USDT",
    position_side: str = "LONG",
    order_side: str = "BUY",
    reduce_only: bool = False,
    quantity: str = "100",
    value: str = "7.917",
    fee: str = "0.0063336",
    realize_pnl: str = "0",
    seconds: int = 0,
) -> FillLeg:
    return FillLeg(
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        symbol=symbol,
        position_side=position_side,
        order_side=order_side,
        quantity=Decimal(quantity),
        value=Decimal(value),
        fee=Decimal(fee),
        realize_pnl=Decimal(realize_pnl),
        filled_at=BASE + timedelta(seconds=seconds),
        reduce_only=reduce_only,
    )


def test_pairs_a_single_open_and_close():
    """库里那条真实往返的形状：开 BUY 100 @7.917、平 SELL 100 @7.918。"""

    open_leg = _leg("f-open")
    close_leg = _leg(
        "f-close",
        order_side="SELL",
        reduce_only=True,
        value="7.918",
        fee="0.0063344",
        realize_pnl="0.001",
        seconds=29,
    )

    trips, unmatched = match_round_trips([open_leg, close_leg])

    assert unmatched == []
    assert len(trips) == 1
    trip = trips[0]
    assert trip.symbol == "DOGE-USDT"
    assert trip.position_side == "LONG"
    assert trip.quantity == Decimal(100)
    assert trip.open_legs == (open_leg,)
    assert trip.close_leg is close_leg
    assert trip.realize_pnl == Decimal("0.001")
    assert trip.fee == Decimal("0.012668")          # 两腿之和
    assert trip.pnl_net == Decimal("-0.011668")     # 0.001 − 0.012668
    assert trip.open_avg == Decimal("0.07917")
    assert trip.close_avg == Decimal("0.07918")


def test_close_without_an_open_leg_is_reported_as_unmatched():
    """监听上线前就开着的仓被平掉时会出现这种腿。

    **不能猜** —— 猜成开仓腿会污染后续所有配对。报给调用方，由它决定怎么办。
    """

    close_leg = _leg("f-close", order_side="SELL", reduce_only=True, realize_pnl="0.5")

    trips, unmatched = match_round_trips([close_leg])

    assert trips == []
    assert unmatched == [close_leg]


def test_open_without_a_close_is_not_reported_anywhere():
    """还开着的仓不是异常，只是这笔往返还没结束。"""

    open_leg = _leg("f-open")

    trips, unmatched = match_round_trips([open_leg])

    assert trips == []
    assert unmatched == []


def test_consumes_open_legs_in_time_order():
    """FIFO：先开的先配。"""

    first = _leg("f-1", quantity="60", value="6", fee="0.006", seconds=0)
    second = _leg("f-2", quantity="40", value="4", fee="0.004", seconds=10)
    close_leg = _leg(
        "f-3", order_side="SELL", reduce_only=True, quantity="100", value="10.1",
        fee="0.008", realize_pnl="0.1", seconds=20,
    )

    trips, unmatched = match_round_trips([second, first, close_leg])  # 故意乱序传入

    assert unmatched == []
    assert len(trips) == 1
    trip = trips[0]
    assert trip.open_legs == (first, second)
    assert trip.open_value == Decimal(10)       # 6 + 4
    assert trip.open_fee == Decimal("0.010")    # 0.006 + 0.004
    assert trip.fee == Decimal("0.018")         # + 平仓腿 0.008


def test_partial_close_apportions_open_fee_by_quantity():
    """部分平仓时开仓腿要被拆开，费用按数量比例分摊。

    fee 与名义价值成正比、名义价值与数量成正比，所以按数量分摊是精确的。
    """

    open_leg = _leg("f-open", quantity="100", value="8", fee="0.008")
    first_close = _leg(
        "f-c1", order_side="SELL", reduce_only=True, quantity="40", value="3.2",
        fee="0.0032", realize_pnl="0.01", seconds=10,
    )
    second_close = _leg(
        "f-c2", order_side="SELL", reduce_only=True, quantity="60", value="4.8",
        fee="0.0048", realize_pnl="0.02", seconds=20,
    )

    trips, unmatched = match_round_trips([open_leg, first_close, second_close])

    assert unmatched == []
    assert len(trips) == 2
    assert trips[0].close_leg is first_close
    assert trips[0].open_fee == Decimal("0.0032")     # 0.008 × 40/100
    assert trips[0].open_value == Decimal("3.2")      # 8 × 40/100
    assert trips[0].fee == Decimal("0.0064")
    assert trips[1].open_fee == Decimal("0.0048")     # 0.008 × 60/100
    assert trips[1].fee == Decimal("0.0096")


def test_close_larger_than_available_open_quantity_is_unmatched():
    """平仓量大于能配到的开仓量 —— 配不上的部分不能凭空补。"""

    open_leg = _leg("f-open", quantity="40")
    close_leg = _leg(
        "f-close", order_side="SELL", reduce_only=True, quantity="100", value="8",
        realize_pnl="0.1", seconds=10,
    )

    trips, unmatched = match_round_trips([open_leg, close_leg])

    assert trips == []
    assert unmatched == [close_leg]


def test_different_symbols_do_not_cross_match():
    open_doge = _leg("f-1", symbol="DOGE-USDT")
    close_xrp = _leg(
        "f-2", symbol="XRP-USDT", order_side="SELL", reduce_only=True, seconds=10
    )

    trips, unmatched = match_round_trips([open_doge, close_xrp])

    assert trips == []
    assert unmatched == [close_xrp]


def test_long_and_short_do_not_cross_match():
    """同一品种的多空是两条独立的账，不能互相配。"""

    open_long = _leg("f-1", position_side="LONG")
    close_short = _leg(
        "f-2", position_side="SHORT", order_side="BUY", reduce_only=True, seconds=10
    )

    trips, unmatched = match_round_trips([open_long, close_short])

    assert trips == []
    assert unmatched == [close_short]


def test_open_leg_with_zero_quantity_is_a_hard_error():
    """数量为 0 的成交是数据异常。按 0 分摊会抛 ZeroDivisionError ——
    宁可炸掉也不要静默产出一个费用被算成 0 的叶子。"""

    import pytest

    open_leg = _leg("f-open", quantity="0")
    close_leg = _leg(
        "f-close", order_side="SELL", reduce_only=True, quantity="0", seconds=10
    )

    with pytest.raises(ZeroDivisionError):
        match_round_trips([open_leg, close_leg])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_web3_leaves.py -q`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.web3.leaves'`

- [ ] **Step 3: 写实现**

创建 `backend/app/web3/leaves.py`：

```python
"""把采集层落库的交易所数据组装成待上链的叶子 payload。

**交易所已经给了每笔平仓成交的 ``realizePnl``，所以这里不做盈亏推导。** 配对
（match_round_trips）不是为了算盈亏，而是为了把开仓腿与平仓腿归成一条记录、
拿到往返的总手续费与数量 —— 单条 fill 的 ``realizePnl − fee`` 只是半程。

模块分两层：

- 组装函数（本任务与 Task 3）只吃 dataclass，不碰 SQLAlchemy —— 可纯测
- 查询层（Task 4）把 ORM 行映射成那些 dataclass，是唯一碰 DB 的部分
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class FillLeg:
    """一条成交，加上从 ``exchange_order_events`` 并过来的 ``reduce_only``。

    ``reduce_only`` 只存在于订单事件里（``fill`` 推送没有这个字段），
    所以查询层必须 join，否则分不出开仓腿与平仓腿。
    """

    fill_id: str
    order_id: str
    symbol: str
    position_side: str
    order_side: str
    quantity: Decimal
    value: Decimal
    fee: Decimal
    #: **开仓腿恒为 0**，盈亏全在平仓腿。别拿它当往返盈亏。
    realize_pnl: Decimal
    filled_at: datetime
    reduce_only: bool


@dataclass(frozen=True)
class RoundTrip:
    """一次完整往返：一个平仓腿 + 它消费掉的开仓腿。"""

    symbol: str
    position_side: str
    quantity: Decimal
    open_legs: tuple[FillLeg, ...]
    close_leg: FillLeg
    #: 按消耗数量分摊后的开仓成交额与费用。
    open_value: Decimal
    open_fee: Decimal
    #: 往返总手续费 = 开仓腿分摊部分 + 平仓腿全额。
    fee: Decimal
    #: 平仓腿的交易所回报盈亏（**不含手续费**）。
    realize_pnl: Decimal
    #: 扣掉双边手续费之后的净额 —— 对外展示用的是这个。
    pnl_net: Decimal

    @property
    def open_avg(self) -> Decimal:
        return self.open_value / self.quantity

    @property
    def close_avg(self) -> Decimal:
        return self.close_leg.value / self.close_leg.quantity


@dataclass
class _OpenLot:
    leg: FillLeg
    remaining: Decimal


def match_round_trips(legs: list[FillLeg]) -> tuple[list[RoundTrip], list[FillLeg]]:
    """按 ``(symbol, position_side)`` 分组做 FIFO 配对。

    返回 ``(往返列表, 未能配对上的平仓腿)``。

    **配不上的平仓腿不丢**：它意味着「平掉了一个我们从没观测到开仓的仓位」，
    通常是监听上线之前就持有的仓。那是记录不完整，调用方要知道有多少，
    不能假装没有。反过来，**没被平掉的开仓腿不是异常** —— 那只是这个仓位
    还没结束，不作为未配对返回。
    """

    open_lots: dict[tuple[str, str], list[_OpenLot]] = defaultdict(list)
    trips: list[RoundTrip] = []
    unmatched: list[FillLeg] = []

    for leg in sorted(legs, key=lambda item: item.filled_at):
        key = (leg.symbol, leg.position_side)
        if not leg.reduce_only:
            open_lots[key].append(_OpenLot(leg=leg, remaining=leg.quantity))
            continue

        remaining = leg.quantity
        consumed: list[FillLeg] = []
        open_value = Decimal(0)
        open_fee = Decimal(0)
        while remaining > 0 and open_lots[key]:
            lot = open_lots[key][0]
            take = min(lot.remaining, remaining)
            # 按数量比例分摊：fee 与名义价值成正比，名义价值与数量成正比，
            # 所以这个比例是精确的，不是近似。
            share = take / lot.leg.quantity
            open_value += lot.leg.value * share
            open_fee += lot.leg.fee * share
            consumed.append(lot.leg)
            lot.remaining -= take
            remaining -= take
            if lot.remaining <= 0:
                open_lots[key].pop(0)

        if remaining > 0:
            # 开仓量不够，配不上的部分不能凭空补
            unmatched.append(leg)
            continue

        fee = open_fee + leg.fee
        trips.append(
            RoundTrip(
                symbol=leg.symbol,
                position_side=leg.position_side,
                quantity=leg.quantity,
                open_legs=tuple(consumed),
                close_leg=leg,
                open_value=open_value,
                open_fee=open_fee,
                fee=fee,
                realize_pnl=leg.realize_pnl,
                pnl_net=leg.realize_pnl - fee,
            )
        )

    return trips, unmatched
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_web3_leaves.py -q`

Expected: PASS（9 passed）

- [ ] **Step 5: 静态检查**

Run: `cd backend && uv run ruff check app/web3 tests/unit/test_web3_leaves.py`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add backend/app/web3/leaves.py backend/tests/unit/test_web3_leaves.py
git commit -m "feat(web3): 成交腿 FIFO 配对

配对不是为了算盈亏 —— 交易所已经在平仓腿上给了 realizePnl。配对是为了把
两腿归成一条记录，拿到往返的总手续费与数量：单条 fill 的 realizePnl − fee
只是半程（开仓腿的 realizePnl 恒为 0）。

按 (symbol, position_side) 分组做 FIFO，一个平仓腿可以消费多个开仓腿，
一条开仓腿也可以被多次部分平仓拆开。费用与成交额按数量比例分摊，这个比例
是精确的（fee ∝ 名义价值 ∝ 数量），不是近似。

配不上的平仓腿单独返回而不是丢掉：那意味着「平掉了我们从没观测到开仓的
仓位」，是记录不完整，调用方得知道有多少。反过来没被平掉的开仓腿不是异常，
只是仓位还没结束。"
```

---

## Task 3: 结果叶子与净值叶子的组装

把一次往返 + 它的平仓订单上下文组装成叶子 payload。

**Files:**
- Modify: `backend/app/web3/leaves.py`（追加）
- Test: `backend/tests/unit/test_web3_leaves.py`（追加）

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/unit/test_web3_leaves.py` 顶部把 import 换成（不要在文件中间补
import，那会触发 E402，只能用 `# noqa` 压掉）：

```python
"""成交腿配对、结果叶子与净值叶子的组装。

这些函数都是纯的：吃 dataclass，吐 dataclass / dict。查询层（Task 4）单独测。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.web3.leaves import (
    CloseOrderContext,
    EquitySample,
    FillLeg,
    account_reference,
    build_equity_leaf,
    build_result_leaf,
    match_round_trips,
)
from app.web3.payload import SCHEMA, canonical_json, leaf_hash
```

然后在文件末尾追加：

```python
def _close_order(**overrides) -> CloseOrderContext:
    values = {
        "order_source": "UNKNOWN_ORDER_SOURCE",
        "client_order_id": "1b7de35c-6d81-43c6-8fbb-671713a7c86b",
        "reduce_only": True,
        "trigger_price": Decimal(0),
        "cum_liquidate_fee": Decimal(0),
    }
    values.update(overrides)
    return CloseOrderContext(**values)


def _trip():
    open_leg = _leg("f-open")
    close_leg = _leg(
        "f-close",
        order_side="SELL",
        reduce_only=True,
        value="7.918",
        fee="0.0063344",
        realize_pnl="0.001",
        seconds=29,
    )
    trips, _ = match_round_trips([open_leg, close_leg])
    return trips[0]


def test_account_reference_is_stable_and_hides_the_account_id():
    """叶子进 IPFS 后可能被用户主动公开，内部账户 UUID 没必要跟着出去。

    但要留一个稳定的摘要，让第三方能确认「这些叶子来自同一个账户」。
    """

    reference = account_reference("973f4ac1-afd3-459e-a3c9-678e2982c2ba")

    assert reference == account_reference("973f4ac1-afd3-459e-a3c9-678e2982c2ba")
    assert reference.startswith("0x")
    assert len(reference) == 18          # 0x + 16 位十六进制
    assert "973f4ac1" not in reference
    assert account_reference("other-account") != reference


def test_result_leaf_carries_the_round_trip_numbers():
    payload = build_result_leaf(
        _trip(), account_ref="0xabc", order=_close_order(), signal_hash=None
    )

    assert payload["schema"] == SCHEMA
    assert payload["kind"] == "result"
    assert payload["symbol"] == "DOGE-USDT"
    assert payload["side"] == "LONG"
    assert payload["quantity"] == Decimal(100)
    assert payload["open_avg"] == Decimal("0.07917")
    assert payload["close_avg"] == Decimal("0.07918")
    assert payload["fee"] == Decimal("0.012668")
    assert payload["realize_pnl"] == Decimal("0.001")
    assert payload["pnl_net"] == Decimal("-0.011668")
    assert payload["pnl_source"] == "exchange-reported"
    assert payload["open_fill_ids"] == ["f-open"]
    assert payload["close_fill_id"] == "f-close"


def test_result_leaf_without_a_signal_is_not_followed():
    payload = build_result_leaf(_trip(), account_ref="0xabc", order=_close_order())

    assert payload["signal_hash"] is None
    assert payload["followed"] is False
    assert payload["attribution"] == "none"


def test_result_leaf_with_a_signal_is_followed():
    payload = build_result_leaf(
        _trip(), account_ref="0xabc", order=_close_order(), signal_hash="0xdead"
    )

    assert payload["signal_hash"] == "0xdead"
    assert payload["followed"] is True
    assert payload["attribution"] == "matched"


def test_exit_reason_comes_from_the_order_source():
    """条件单与手动平仓要能分开 —— 这决定了 P2 怎么归因。"""

    take_profit = build_result_leaf(
        _trip(), account_ref="0xabc",
        order=_close_order(order_source="STOP_PROFIT"),
    )
    stop_loss = build_result_leaf(
        _trip(), account_ref="0xabc",
        order=_close_order(order_source="STOP_LOSS"),
    )
    manual = build_result_leaf(_trip(), account_ref="0xabc", order=_close_order())

    assert take_profit["exit_reason"] == "take_profit"
    assert stop_loss["exit_reason"] == "stop_loss"
    assert manual["exit_reason"] == "manual"


def test_liquidation_is_recognised_by_the_liquidate_fee():
    payload = build_result_leaf(
        _trip(),
        account_ref="0xabc",
        order=_close_order(order_source="UNKNOWN_ORDER_SOURCE", cum_liquidate_fee=Decimal("0.5")),
    )

    assert payload["exit_reason"] == "liquidation"


def test_missing_order_context_does_not_guess():
    """订单事件没采到时不能假装是手动平仓 —— 标 unknown，让下游知道这里不确定。"""

    payload = build_result_leaf(_trip(), account_ref="0xabc", order=None)

    assert payload["exit_reason"] == "unknown"
    assert payload["order_source"] == ""
    assert payload["client_order_id"] == ""
    assert payload["trigger_price"] is None


def test_trigger_price_is_kept_for_conditional_orders():
    payload = build_result_leaf(
        _trip(),
        account_ref="0xabc",
        order=_close_order(order_source="STOP_PROFIT", trigger_price=Decimal("0.0799")),
    )

    assert payload["trigger_price"] == Decimal("0.0799")


def test_same_trip_always_hashes_the_same():
    """同一条往返算两次必须得到同一个哈希 —— 否则链上锚定没有意义。"""

    first = build_result_leaf(_trip(), account_ref="0xabc", order=_close_order())
    second = build_result_leaf(_trip(), account_ref="0xabc", order=_close_order())

    assert canonical_json(first) == canonical_json(second)
    assert leaf_hash(first) == leaf_hash(second)


def test_different_trips_hash_differently():
    other = build_result_leaf(
        _trip(), account_ref="0xabc", order=_close_order(order_source="STOP_PROFIT")
    )

    assert leaf_hash(other) != leaf_hash(
        build_result_leaf(_trip(), account_ref="0xabc", order=_close_order())
    )


def _equity_sample(**overrides) -> EquitySample:
    values = {
        "coin": "SUSDT",
        "amount": Decimal("59010.73187629"),
        "cum_deposit": Decimal(0),
        "cum_withdraw": Decimal(0),
        "cum_transfer_in": Decimal("40000.00000000"),
        "cum_transfer_out": Decimal(0),
        "cum_fill_fee": Decimal("0.00639200"),
        "cum_funding_fee": Decimal("-56.35171893"),
        "account_version": 703,
        "captured_at": BASE,
    }
    values.update(overrides)
    return EquitySample(**values)


def test_equity_leaf_carries_the_funding_flows():
    """资金进出字段必须在 —— 这是「净值增长不是靠充值做出来的」的唯一依据。"""

    payload = build_equity_leaf(
        _equity_sample(), account_ref="0xabc", window_sample_count=7
    )

    assert payload["schema"] == SCHEMA
    assert payload["kind"] == "equity"
    assert payload["amount"] == Decimal("59010.73187629")
    assert payload["cum_deposit"] == Decimal(0)
    assert payload["cum_transfer_in"] == Decimal("40000.00000000")
    assert payload["cum_fill_fee"] == Decimal("0.00639200")
    assert payload["cum_funding_fee"] == Decimal("-56.35171893")
    assert payload["account_version"] == 703
    assert payload["window_sample_count"] == 7
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_web3_leaves.py -q`

Expected: FAIL — `ImportError: cannot import name 'CloseOrderContext' from 'app.web3.leaves'`

- [ ] **Step 3: 写实现**

在 `backend/app/web3/leaves.py` 的 import 区**新增**下面这几行（Task 2 已经导入过
`dataclass` / `datetime` / `Decimal` / `defaultdict`，不要重复导入 —— ruff 会报
F811）：

```python
from typing import Any

from eth_utils import keccak

from app.streams.protocol import exit_reason
from app.web3.payload import SCHEMA
```

> `exit_reason` 复用采集层的实现而不是重写一份：它编码的是「`orderSource` 枚举不统一」
> 这条实测结论（手动开仓是 `WEB`、手动平仓是 `UNKNOWN_ORDER_SOURCE`、条件单是
> `STOP_LOSS`/`STOP_PROFIT`），两处各写一份迟早会漂移。

并在文件末尾追加：

```python
def account_reference(account_id: str) -> str:
    """账户的假名引用：``keccak256(account_id)`` 的前 8 字节。

    叶子进 IPFS 后可以被用户主动公开，内部账户 UUID 没必要跟着出去；但又要让
    第三方能确认「这些叶子来自同一个账户」，所以留一个稳定的摘要。

    **函数名刻意不叫 ``account_ref``** —— 下面的 ``build_result_leaf`` 有个同名的
    关键字参数 ``account_ref``，重名会在函数体里遮蔽这个函数。现在没踩到，
    但那是定时炸弹。
    """

    return "0x" + keccak(account_id.encode("utf-8")).hex()[:16]


@dataclass(frozen=True)
class CloseOrderContext:
    """平仓腿那笔委托的信息。从 ``exchange_order_events`` 来。"""

    order_source: str
    client_order_id: str
    reduce_only: bool
    trigger_price: Decimal
    cum_liquidate_fee: Decimal


def build_result_leaf(
    trip: RoundTrip,
    *,
    account_ref: str,
    order: CloseOrderContext | None = None,
    signal_hash: str | None = None,
) -> dict[str, Any]:
    """② 结果叶子的 payload（明文）。

    ``order`` 为 ``None`` 表示订单事件没采到 —— 那时 ``exit_reason`` 记
    ``"unknown"`` 而**不是**猜测。猜成「手动平仓」会让 P2 的归因统计悄悄失真。
    """

    if order is None:
        reason = "unknown"
    else:
        reason = exit_reason(
            order_source=order.order_source,
            reduce_only=order.reduce_only,
            cum_liquidate_fee=order.cum_liquidate_fee,
        )
    return {
        "schema": SCHEMA,
        "kind": "result",
        "account_ref": account_ref,
        "signal_hash": signal_hash,
        "followed": signal_hash is not None,
        "attribution": "matched" if signal_hash is not None else "none",
        "symbol": trip.symbol,
        "side": trip.position_side,
        "quantity": trip.quantity,
        "open_avg": trip.open_avg,
        "close_avg": trip.close_avg,
        # 开仓腿按时间序消费，所以第一条就是最早的那笔
        "open_at": trip.open_legs[0].filled_at,
        "close_at": trip.close_leg.filled_at,
        "open_fill_ids": [leg.fill_id for leg in trip.open_legs],
        "close_fill_id": trip.close_leg.fill_id,
        "order_source": order.order_source if order else "",
        "client_order_id": order.client_order_id if order else "",
        "exit_reason": reason,
        "trigger_price": order.trigger_price if order else None,
        "open_fee": trip.open_fee,
        "close_fee": trip.close_leg.fee,
        "fee": trip.fee,
        "realize_pnl": trip.realize_pnl,
        "pnl_net": trip.pnl_net,
        "pnl_source": "exchange-reported",
    }


@dataclass(frozen=True)
class EquitySample:
    """净值采样。从 ``exchange_account_samples`` 来。"""

    coin: str
    amount: Decimal
    cum_deposit: Decimal
    cum_withdraw: Decimal
    cum_transfer_in: Decimal
    cum_transfer_out: Decimal
    cum_fill_fee: Decimal
    cum_funding_fee: Decimal
    account_version: int
    captured_at: datetime


def build_equity_leaf(
    sample: EquitySample,
    *,
    account_ref: str,
    window_sample_count: int,
) -> dict[str, Any]:
    """⑤ 净值叶子的 payload（明文）。

    ``window_sample_count`` 是这批被浓缩掉的采样条数 —— 链上只记窗口末尾那一条，
    第三方需要知道它代表了多少次观测。
    """

    return {
        "schema": SCHEMA,
        "kind": "equity",
        "account_ref": account_ref,
        "captured_at": sample.captured_at,
        "coin": sample.coin,
        "amount": sample.amount,
        "cum_deposit": sample.cum_deposit,
        "cum_withdraw": sample.cum_withdraw,
        "cum_transfer_in": sample.cum_transfer_in,
        "cum_transfer_out": sample.cum_transfer_out,
        "cum_fill_fee": sample.cum_fill_fee,
        "cum_funding_fee": sample.cum_funding_fee,
        "account_version": sample.account_version,
        "window_sample_count": window_sample_count,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_web3_leaves.py -q`

Expected: PASS（20 passed —— Task 2 的 9 条加上本任务的 11 条）

- [ ] **Step 5: 静态检查**

Run: `cd backend && uv run ruff check app/web3 tests/unit/test_web3_leaves.py`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add backend/app/web3/leaves.py backend/tests/unit/test_web3_leaves.py
git commit -m "feat(web3): 结果叶子与净值叶子的组装

结果叶子把一次往返 + 平仓订单上下文变成 payload。exit_reason 复用采集层
的 exit_reason() 而不是重写一份 —— 它编码的是「orderSource 枚举不统一」这条
实测结论，两处各写一份迟早漂移。

订单事件没采到时 exit_reason 记 unknown 而不是猜成「手动平仓」：猜错会让
P2 的归因统计悄悄失真，而 unknown 只是让那一条无法归因。

account_ref 用 keccak 前 8 字节：叶子进 IPFS 后可以被用户主动公开，内部
账户 UUID 没必要跟着出去，但要留个稳定摘要让第三方能确认叶子同源。

净值叶子带 cum_deposit / cum_withdraw / cum_transfer_in —— 这是「净值增长
不是靠充值做出来的」的唯一依据。"
```

---

## Task 4: 查询层

把 ORM 行映射成 Task 2–3 的 dataclass，并提供「给一个窗口，产出这一批叶子」的入口。

**Files:**
- Modify: `backend/app/web3/leaves.py`（追加）
- Test: `backend/tests/unit/test_web3_leaves_query.py`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/unit/test_web3_leaves_query.py`：

```python
"""查询层：把 ORM 行映射成叶子的输入。

这里用的是真实表结构，所以它同时验证「代码假设的列名与类型」对不对 ——
纯函数那层测不出这个。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    CollectorError,
    ExchangeAccountSample,
    ExchangeFillRecord,
    ExchangeOrderEvent,
    User,
)
from app.streams.reconciler import GAP_COLLECTOR, GAP_MESSAGE
from app.web3.leaves import build_leaves_for_window

ACCOUNT = "acct-1"
WINDOW_START = datetime(2026, 10, 15, 13, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC)
OPENED_AT = datetime(2026, 10, 15, 13, 23, 28, 667000, tzinfo=UTC)
CLOSED_AT = datetime(2026, 10, 15, 13, 23, 57, 408000, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'leaves.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="u-1", clerk_user_id="clerk-u1"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _fill(db: Session, *, fill_id: str, order_id: str, **overrides) -> None:
    values = {
        "symbol": "DOGE-USDT",
        "position_side": "LONG",
        "order_side": "BUY",
        "quantity": Decimal(100),
        "value": Decimal("7.917"),
        "fee": Decimal("0.0063336"),
        "realize_pnl": Decimal(0),
        "filled_at": OPENED_AT,
    }
    values.update(overrides)
    db.add(
        ExchangeFillRecord(
            id=str(uuid4()),
            user_id="u-1",
            trading_account_id=ACCOUNT,
            exchange_fill_id=fill_id,
            exchange_order_id=order_id,
            direction="TAKER",
            account_version=1,
            raw={},
            **values,
        )
    )
    db.commit()


def _order(db: Session, *, order_id: str, **overrides) -> None:
    values = {
        "client_order_id": f"cid-{order_id}",
        "symbol": "DOGE-USDT",
        "position_side": "LONG",
        "order_side": "BUY",
        "order_type": "MARKET",
        "status": "FILLED",
        "order_source": "WEB",
        "reduce_only": False,
        "position_tpsl": False,
        "trigger_price": Decimal(0),
        "quantity": Decimal(100),
        "cum_fill_quantity": Decimal(100),
        "cum_fill_value": Decimal("7.917"),
        "cum_fill_fee": Decimal("0.0063336"),
        "cum_realize_pnl": Decimal(0),
        "cum_liquidate_fee": Decimal(0),
        "taker_fee_rate": Decimal("0.0008"),
        "maker_fee_rate": Decimal("0.0002"),
        "account_version": 1,
        "exchange_created_at": OPENED_AT,
        "exchange_updated_at": OPENED_AT,
        "raw": {},
    }
    values.update(overrides)
    db.add(
        ExchangeOrderEvent(
            id=str(uuid4()),
            user_id="u-1",
            trading_account_id=ACCOUNT,
            exchange_order_id=order_id,
            **values,
        )
    )
    db.commit()


def _seed_round_trip(db: Session) -> None:
    """库里那条真实往返的形状。"""

    _order(db, order_id="o-open", order_source="WEB", reduce_only=False)
    _order(
        db,
        order_id="o-close",
        order_side="SELL",
        order_source="UNKNOWN_ORDER_SOURCE",
        reduce_only=True,
        exchange_created_at=CLOSED_AT,
        exchange_updated_at=CLOSED_AT,
    )
    _fill(db, fill_id="f-open", order_id="o-open")
    _fill(
        db,
        fill_id="f-close",
        order_id="o-close",
        order_side="SELL",
        value=Decimal("7.918"),
        fee=Decimal("0.0063344"),
        realize_pnl=Decimal("0.001"),
        filled_at=CLOSED_AT,
    )


def _seed_sample(db: Session, *, captured_at: datetime, version: int, amount: str) -> None:
    db.add(
        ExchangeAccountSample(
            id=str(uuid4()),
            user_id="u-1",
            trading_account_id=ACCOUNT,
            coin="SUSDT",
            amount=Decimal(amount),
            cum_deposit=Decimal(0),
            cum_withdraw=Decimal(0),
            cum_transfer_in=Decimal("40000"),
            cum_transfer_out=Decimal(0),
            cum_fill_fee=Decimal("0.006392"),
            cum_funding_fee=Decimal("-56.35171893"),
            liquidating=False,
            account_version=version,
            captured_at=captured_at,
            raw={},
        )
    )
    db.commit()


def test_builds_one_result_leaf_from_the_round_trip(db):
    _seed_round_trip(db)

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert len(batch.result_leaves) == 1
    leaf = batch.result_leaves[0]
    assert leaf["symbol"] == "DOGE-USDT"
    assert leaf["exit_reason"] == "manual"
    assert leaf["order_source"] == "UNKNOWN_ORDER_SOURCE"
    assert leaf["fee"] == Decimal("0.012668")
    assert leaf["pnl_net"] == Decimal("-0.011668")
    assert batch.unmatched_close_legs == []


def test_joins_reduce_only_from_the_order_event(db):
    """``reduce_only`` 只在订单事件里，fill 没有 —— 不 join 就分不出开仓与平仓。

    这条测试的意义是：如果哪天有人把 join 去掉，「结果叶子数」会从 1 变成 0
    （两条腿都被当成开仓），而不是报错。
    """

    _seed_round_trip(db)

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert len(batch.result_leaves) == 1


def test_skips_fills_whose_symbol_was_never_backfilled(db):
    """推送里没有 symbol，靠对账回填。还没回填的不能进叶子（spec §16.3）。"""

    _seed_round_trip(db)
    row = db.query(ExchangeFillRecord).filter_by(exchange_fill_id="f-close").one()
    row.symbol = None
    db.commit()

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert batch.result_leaves == []
    assert batch.skipped_null_symbol == 1


def test_counts_fills_without_an_order_event(db):
    """订单事件缺失时无法判断开仓还是平仓 —— 跳过并计数，不猜。"""

    _seed_round_trip(db)
    db.query(ExchangeOrderEvent).filter_by(exchange_order_id="o-close").delete()
    db.commit()

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert batch.result_leaves == []
    assert batch.skipped_missing_order == 1


def test_reports_unmatched_close_legs(db):
    """监听上线前就持有的仓被平掉 —— 记录不完整，要让调用方看见。"""

    _order(db, order_id="o-close", order_side="SELL", reduce_only=True)
    _fill(db, fill_id="f-close", order_id="o-close", order_side="SELL", realize_pnl=Decimal("1"))

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert batch.result_leaves == []
    assert [leg.fill_id for leg in batch.unmatched_close_legs] == ["f-close"]


def test_equity_leaf_uses_the_last_sample_in_the_window(db):
    """链上只记窗口末尾那一条，并注明它浓缩了多少次观测。"""

    _seed_sample(db, captured_at=WINDOW_START + timedelta(minutes=1), version=1, amount="100")
    _seed_sample(db, captured_at=WINDOW_START + timedelta(minutes=5), version=2, amount="200")
    _seed_sample(db, captured_at=WINDOW_START + timedelta(minutes=9), version=3, amount="300")
    # 窗口之外，不该被算进去
    _seed_sample(db, captured_at=WINDOW_END + timedelta(minutes=1), version=4, amount="999")

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert batch.equity_leaf is not None
    assert batch.equity_leaf["amount"] == Decimal("300")
    assert batch.equity_leaf["account_version"] == 3
    assert batch.equity_leaf["window_sample_count"] == 3


def test_no_equity_leaf_when_the_window_has_no_sample(db):
    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert batch.equity_leaf is None


def test_ignores_other_accounts(db):
    _seed_round_trip(db)

    batch = build_leaves_for_window(
        db, trading_account_id="someone-else", since=WINDOW_START, until=WINDOW_END
    )

    assert batch.result_leaves == []


def test_ignores_fills_outside_the_window(db):
    _seed_round_trip(db)

    batch = build_leaves_for_window(
        db,
        trading_account_id=ACCOUNT,
        since=WINDOW_START + timedelta(hours=1),
        until=WINDOW_END + timedelta(hours=1),
    )

    assert batch.result_leaves == []


def _seed_gap(db: Session, *, first_seen: datetime, last_seen: datetime) -> None:
    db.add(
        CollectorError(
            collector=GAP_COLLECTOR,
            message=GAP_MESSAGE,
            occurrences=3,
            first_seen_at=first_seen,
            last_seen_at=last_seen,
        )
    )
    db.commit()


def test_flags_the_window_when_an_observation_gap_was_recorded(db):
    """上游 spec §16.3：窗口内有漏记时，批次必须带「不完整」标记。

    漏记必须可被看见 —— 否则第三方会以为那段时间真的没有交易。
    """

    _seed_round_trip(db)
    _seed_gap(db, first_seen=WINDOW_START, last_seen=WINDOW_START + timedelta(minutes=1))

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert batch.observation_gap_seen is True


def test_no_gap_flag_when_the_gap_is_outside_the_window(db):
    _seed_round_trip(db)
    _seed_gap(
        db,
        first_seen=WINDOW_START - timedelta(hours=3),
        last_seen=WINDOW_START - timedelta(hours=2),
    )

    batch = build_leaves_for_window(
        db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
    )

    assert batch.observation_gap_seen is False


def test_no_gap_flag_when_nothing_was_recorded(db):
    assert (
        build_leaves_for_window(
            db, trading_account_id=ACCOUNT, since=WINDOW_START, until=WINDOW_END
        ).observation_gap_seen
        is False
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/unit/test_web3_leaves_query.py -q`

Expected: FAIL — `ImportError: cannot import name 'build_leaves_for_window' from 'app.web3.leaves'`

- [ ] **Step 3: 写实现**

在 `backend/app/web3/leaves.py` 的 import 区**新增**下面这几行（`dataclass` 在
Task 2 已导入，只补 `field`）：

```python
from dataclasses import field

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.db.models import (
    CollectorError,
    ExchangeAccountSample,
    ExchangeFillRecord,
    ExchangeOrderEvent,
)
from app.streams.reconciler import GAP_COLLECTOR, GAP_MESSAGE
```

> 那两个常量从采集层 import 而不是抄一份字符串：抄的话，哪天采集层改了
> message，这边会静默地永远读不到断档记录。没有循环依赖 —— `reconciler`
> 不 import `app.web3`。

并在文件末尾追加：

```python
@dataclass(frozen=True)
class LegLoadResult:
    legs: list[FillLeg]
    #: order_id → 平仓订单上下文。配对后由调用方按 ``close_leg.order_id`` 取。
    orders: dict[str, CloseOrderContext]
    #: 这两类跳过都要计数 —— 静默丢弃会让「这批叶子是全的」这个判断失真。
    skipped_missing_order: int = 0
    skipped_null_symbol: int = 0


def load_fill_legs(
    db: Session,
    *,
    trading_account_id: str,
    since: datetime,
    until: datetime,
) -> LegLoadResult:
    """读窗口内的成交腿，并把订单事件的 ``reduce_only`` 并进来。

    **必须 join**：``reduce_only`` 只存在于 ``exchange_order_events``
    （``fill`` 推送没有这个字段），不 join 就分不出开仓腿与平仓腿。
    """

    rows = db.execute(
        select(ExchangeFillRecord, ExchangeOrderEvent)
        .outerjoin(
            ExchangeOrderEvent,
            and_(
                ExchangeOrderEvent.trading_account_id == ExchangeFillRecord.trading_account_id,
                ExchangeOrderEvent.exchange_order_id == ExchangeFillRecord.exchange_order_id,
            ),
        )
        .where(
            ExchangeFillRecord.trading_account_id == trading_account_id,
            ExchangeFillRecord.filled_at >= since,
            ExchangeFillRecord.filled_at < until,
        )
        .order_by(ExchangeFillRecord.filled_at)
    ).all()

    legs: list[FillLeg] = []
    orders: dict[str, CloseOrderContext] = {}
    missing_order = 0
    null_symbol = 0
    for fill, event in rows:
        if not fill.symbol:
            # 推送里没有 symbol，靠 REST 对账回填；还没回填的不能进叶子
            null_symbol += 1
            continue
        if event is None:
            # 判断不了开仓还是平仓 —— 跳过而不是默认当成开仓，
            # 那会让配对结果悄悄错掉
            missing_order += 1
            continue
        legs.append(
            FillLeg(
                fill_id=fill.exchange_fill_id,
                order_id=fill.exchange_order_id,
                symbol=fill.symbol,
                position_side=fill.position_side,
                order_side=fill.order_side,
                quantity=fill.quantity,
                value=fill.value,
                fee=fill.fee,
                realize_pnl=fill.realize_pnl,
                filled_at=fill.filled_at,
                reduce_only=bool(event.reduce_only),
            )
        )
        orders[fill.exchange_order_id] = CloseOrderContext(
            order_source=event.order_source,
            client_order_id=event.client_order_id,
            reduce_only=bool(event.reduce_only),
            trigger_price=event.trigger_price,
            cum_liquidate_fee=event.cum_liquidate_fee,
        )
    return LegLoadResult(
        legs=legs,
        orders=orders,
        skipped_missing_order=missing_order,
        skipped_null_symbol=null_symbol,
    )


def _load_equity(
    db: Session, *, trading_account_id: str, since: datetime, until: datetime
) -> tuple[EquitySample | None, int]:
    """窗口内最后一条净值采样，以及这个窗口一共有多少条采样。

    链上只记末尾那一条 —— 每次账户变动都上一次链没有意义，而第三方需要知道
    它浓缩了多少次观测。
    """

    rows = db.scalars(
        select(ExchangeAccountSample)
        .where(
            ExchangeAccountSample.trading_account_id == trading_account_id,
            ExchangeAccountSample.captured_at >= since,
            ExchangeAccountSample.captured_at < until,
        )
        .order_by(ExchangeAccountSample.captured_at)
    ).all()
    if not rows:
        return None, 0
    last = rows[-1]
    return (
        EquitySample(
            coin=last.coin,
            amount=last.amount,
            cum_deposit=last.cum_deposit,
            cum_withdraw=last.cum_withdraw,
            cum_transfer_in=last.cum_transfer_in,
            cum_transfer_out=last.cum_transfer_out,
            cum_fill_fee=last.cum_fill_fee,
            cum_funding_fee=last.cum_funding_fee,
            account_version=last.account_version,
            captured_at=last.captured_at,
        ),
        len(rows),
    )


@dataclass(frozen=True)
class LeafBatch:
    """一个窗口产出的全部叶子。"""

    result_leaves: list[dict[str, Any]] = field(default_factory=list)
    equity_leaf: dict[str, Any] | None = None
    #: 平掉了但从没观测到开仓的腿 —— 记录不完整，调用方要能看见。
    unmatched_close_legs: list[FillLeg] = field(default_factory=list)
    #: 这两类跳过都要计数 —— 静默丢弃会让「这批叶子是全的」这个判断失真。
    skipped_missing_order: int = 0
    skipped_null_symbol: int = 0
    #: 窗口期间出现过观测断档（采集层漏记了订单）。上游 spec §16.3 要求这种
    #: 批次在链上带「不完整」标记 —— 漏记必须可被看见，不能假装完整。
    observation_gap_seen: bool = False


def _observation_gap_seen(
    db: Session, *, since: datetime, until: datetime
) -> bool:
    """窗口期间有没有记过观测断档。

    ``collector_errors`` 是**归并计数**表（一条记录累计 ``occurrences``），
    没有每次发生的时刻。所以这里只能按 ``first_seen_at`` / ``last_seen_at``
    与窗口是否相交来判断 —— 精度有限，宁可多标「不完整」，也不要漏标。
    采集层的 ``observation_gap`` 用的是同一个 ``GAP_COLLECTOR`` / ``GAP_MESSAGE``。
    """

    row = db.scalar(
        select(CollectorError).where(
            CollectorError.collector == GAP_COLLECTOR,
            CollectorError.message == GAP_MESSAGE,
            CollectorError.last_seen_at >= since,
            CollectorError.first_seen_at < until,
        )
    )
    return row is not None


def build_leaves_for_window(
    db: Session,
    *,
    trading_account_id: str,
    since: datetime,
    until: datetime,
) -> LeafBatch:
    """把一个窗口的交易所数据组装成待上链叶子。"""

    reference = account_reference(trading_account_id)
    loaded = load_fill_legs(
        db, trading_account_id=trading_account_id, since=since, until=until
    )
    trips, unmatched = match_round_trips(loaded.legs)
    sample, sample_count = _load_equity(
        db, trading_account_id=trading_account_id, since=since, until=until
    )
    return LeafBatch(
        result_leaves=[
            build_result_leaf(
                trip,
                account_ref=reference,
                order=loaded.orders.get(trip.close_leg.order_id),
            )
            for trip in trips
        ],
        equity_leaf=(
            build_equity_leaf(sample, account_ref=reference, window_sample_count=sample_count)
            if sample is not None
            else None
        ),
        unmatched_close_legs=unmatched,
        skipped_missing_order=loaded.skipped_missing_order,
        skipped_null_symbol=loaded.skipped_null_symbol,
        observation_gap_seen=_observation_gap_seen(db, since=since, until=until),
    )
```

> 这里**没有**填 ``signal_hash`` —— 信号承诺（① 类叶子）要接入 Lark 推送路径，
> 是本计划之外的另一份计划。现在 ``followed`` 恒为 ``false``、``attribution``
> 恒为 ``"none"``，那是诚实的：确实还没做归因。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/unit/test_web3_leaves_query.py -q`

Expected: PASS（12 passed）

- [ ] **Step 5: 跑全部测试与静态检查**

Run: `cd backend && uv run pytest -q && uv run ruff check app tests workers && uv run mypy app`

Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add backend/app/web3/leaves.py backend/tests/unit/test_web3_leaves_query.py
git commit -m "feat(web3): 叶子的查询层，从采集层的表组装一批叶子

必须 join exchange_order_events：reduce_only 只在订单事件里，fill 推送没有
这个字段，不 join 就分不出开仓腿与平仓腿 —— 而且错法是静默的（结果叶子数
从 1 变成 0，不报错），所以专门写了条测试盯着它。

两类跳过都要计数：symbol 还没被对账回填的、订单事件缺失的。静默丢弃会让
「这批叶子是全的」这个判断失真。配不上的平仓腿也单独返回，那意味着
「平掉了我们从没观测到开仓的仓位」。

净值只取窗口末尾那一条并注明浓缩了多少次采样 —— 每次账户变动都上一次链
没有意义，但第三方需要知道它代表了多少次观测。

signal_hash 现在恒为空、followed 恒为 false：信号承诺要接入 Lark 推送路径，
是另一份计划。现在这样是诚实的 —— 归因确实还没做。"
```

---

## Task 5: 对着真实数据验证

前面所有测试都用构造的数据。这一步用**库里那条真实往返**验证字段映射 —— 它是唯一
能抓住「列名写错、单位搞混、join 条件错」这类问题的手段。

**Files:**
- Create: `backend/scripts/dump_attestation_leaves.py`

- [ ] **Step 1: 写脚本**

创建 `backend/scripts/dump_attestation_leaves.py`：

```python
"""对着真实库打印待上链叶子，人工核对。

**只读**：不写库、不碰 IPFS、不碰链。

用法：

    cd backend && uv run python scripts/dump_attestation_leaves.py --hours 24
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.streams.listener import load_targets
from app.web3.leaves import build_leaves_for_window
from app.web3.payload import canonical_json, leaf_hash


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="打印待上链叶子（只读）")
    parser.add_argument("--hours", type=float, default=24.0, help="回看小时数，默认 24")
    parser.add_argument("--account-id", help="交易账户；只有一个启用账户时可省略")
    parser.add_argument("--raw", action="store_true", help="同时打印完整 payload JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    until = datetime.now(UTC)
    since = until - timedelta(hours=args.hours)

    with SessionLocal() as db:
        account_id = args.account_id
        if not account_id:
            targets = load_targets(db)
            if len(targets) != 1:
                print(f"需要 --account-id（找到 {len(targets)} 个启用账户）", file=sys.stderr)
                for target in targets:
                    print(f"  {target.trading_account_id}", file=sys.stderr)
                return 2
            account_id = targets[0].trading_account_id

        batch = build_leaves_for_window(
            db, trading_account_id=account_id, since=since, until=until
        )

    print(f"账户：{account_id}")
    print(f"窗口：{since.isoformat()} → {until.isoformat()}")
    print(
        f"跳过：symbol 未回填 {batch.skipped_null_symbol} 条，"
        f"缺订单事件 {batch.skipped_missing_order} 条"
    )
    if batch.observation_gap_seen:
        print("⚠️ 窗口期间有观测断档（采集层漏记过订单）—— 这批叶子必须在链上标「不完整」")
    print()

    print(f"=== 结果叶子 {len(batch.result_leaves)} 条 ===")
    for leaf in batch.result_leaves:
        print(
            f"  {leaf['symbol']} {leaf['side']} qty={leaf['quantity']}  "
            f"open={leaf['open_avg']} close={leaf['close_avg']}"
        )
        print(
            f"    毛={leaf['realize_pnl']} 费={leaf['fee']} 净={leaf['pnl_net']}  "
            f"exit_reason={leaf['exit_reason']} source={leaf['order_source']}"
        )
        print(f"    leaf_hash={leaf_hash(leaf)}")
        if args.raw:
            print(f"    payload={json.dumps(json.loads(canonical_json(leaf)), ensure_ascii=False)}")

    print()
    if batch.equity_leaf is None:
        print("=== 净值叶子：窗口内没有采样 ===")
    else:
        leaf = batch.equity_leaf
        print("=== 净值叶子 1 条 ===")
        print(
            f"  {leaf['coin']} amount={leaf['amount']}  "
            f"充值={leaf['cum_deposit']} 划入={leaf['cum_transfer_in']}  "
            f"累计手续费={leaf['cum_fill_fee']} 累计资金费={leaf['cum_funding_fee']}"
        )
        print(f"  窗口内采样 {leaf['window_sample_count']} 条")
        print(f"  leaf_hash={leaf_hash(leaf)}")

    print()
    if batch.unmatched_close_legs:
        print(f"⚠️ 未配对的平仓腿 {len(batch.unmatched_close_legs)} 条（平掉了但没观测到开仓）：")
        for leg in batch.unmatched_close_legs:
            print(f"  {leg.fill_id} {leg.symbol} {leg.position_side} qty={leg.quantity}")
    else:
        print("未配对的平仓腿：无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 跑一次看真实数据**

Run: `cd backend && uv run python scripts/dump_attestation_leaves.py --hours 24`

Expected（对着库里那条真实往返）:

```
=== 结果叶子 1 条 ===
  DOGE-USDT LONG qty=100  open=0.07917 close=0.07918
    毛=0.001 费=0.012668 净=-0.011668  exit_reason=manual source=UNKNOWN_ORDER_SOURCE
    leaf_hash=0x...
```

**逐项核对**：

- `费=0.012668` —— 必须是**两腿之和**（0.0063336 + 0.0063344）。如果这里是
  `0.0063344`，说明 `fee` 只取了平仓腿，那是错的。
- `净=-0.011668` —— `0.001 − 0.012668`。**符号与毛盈亏相反**，这是正常的：
  毛赚了一点点，被双边手续费吃掉。
- `exit_reason=manual`、`source=UNKNOWN_ORDER_SOURCE` —— 手动平仓的
  `orderSource` 实测就是 `UNKNOWN_ORDER_SOURCE`（不是 `WEB`），
  如果这里是 `WEB` 说明拿错了订单上下文。
- 净值叶子里的 `amount` / `累计手续费` 应该与 `exchange_account_samples`
  的最后一条一致。

- [ ] **Step 3: 确认哈希稳定**

Run（连跑两次，哈希必须一样）:

```bash
cd backend && uv run python scripts/dump_attestation_leaves.py --hours 24 | grep leaf_hash
cd backend && uv run python scripts/dump_attestation_leaves.py --hours 24 | grep leaf_hash
```

Expected: 两次的 `leaf_hash` 完全相同。

> 窗口右端是 `now`，会随时间移动；库里那条往返在 24 小时窗口内所以两次都能捞到。
> 若两次窗口没有跨过新的账户变动，哈希应当一致。

- [ ] **Step 4: 静态检查**

Run: `cd backend && uv run ruff check scripts/dump_attestation_leaves.py && uv run mypy app`

Expected: 全绿

- [ ] **Step 5: 跑全部测试确认没有回归**

Run: `cd backend && uv run pytest -q`

Expected: 全部通过（比计划前多 43 条（payload 11 + leaves 20 + query 12））

- [ ] **Step 6: 提交**

```bash
git add backend/scripts/dump_attestation_leaves.py
git commit -m "feat(scripts): 只读打印待上链叶子，对着真实数据核对

前面的测试都用构造数据，这一步用库里那条真实往返验证字段映射 —— 它是唯一
能抓住「列名写错、单位搞混、join 条件错」的手段。

核对点写进了脚本的输出里：费必须是两腿之和（0.0063336 + 0.0063344），
净盈亏符号与毛盈亏相反（毛 +0.001 被双边费 0.012668 吃掉），
平仓的 orderSource 是 UNKNOWN_ORDER_SOURCE 而不是 WEB。"
```

---

## 完成标准

- [ ] `cd backend && uv run pytest -q` 全绿（比计划前多 43 条（payload 11 + leaves 20 + query 12））
- [ ] `cd backend && uv run ruff check app tests workers` 通过
- [ ] `cd backend && uv run mypy app` 无问题
- [ ] `uv run python scripts/dump_attestation_leaves.py --hours 24` 打印出的
      `费=0.012668 净=-0.011668`，与库里那条真实往返一致
- [ ] 连跑两次，`leaf_hash` 完全相同

## 不在本计划范围内

- **信号承诺（① 类叶子）** —— 要接入 Lark 推送路径（`app/services/cycle.py`），
  改的是交易链路，单独一份计划
- **加密与 IPFS** —— 本计划产出的是**明文** payload 与哈希；加密与 pin 是下一份
- **Merkle 树、合约、提交器** —— 需要 Base 测试网
- **`correction` 叶子（④）与 `settlement` 叶子（③）** —— 前者要等真的算错过一次，
  后者要等信号闭环做完


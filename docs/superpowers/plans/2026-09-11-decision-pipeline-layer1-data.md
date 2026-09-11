# 决策链重构 · 第 1 层：数据层 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让决策链拿到它现在拿不到的原料 —— 宏观数值、ticker 的 7 个被丢弃字段、微观结构数据、够深的 K 线 —— 并让外部故障不再熔断账户。

**Architecture:** 采集层补齐字段，落库层改成可自愈的 upsert，新增 context loader 把宏观事实注入 `TradingCycleState`。微观结构单独建模，因为它的失败模式与 ticker 不同。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 / Alembic / pytest。命令都在 `backend/` 下用 `uv` 执行。

**Spec:** `docs/superpowers/specs/2026-09-11-decision-pipeline-redesign-design.md`
**实测依据:** `docs/weex-virtual-api.md`

**通用约定：**
- 每个 Task 结束时提交，提交信息写「为什么」而不是「改了什么」
- 测试全部追加到**已有**测试文件，复用其中既有的桩件，不要另造一套
- 跑测试命令统一 `cd backend && uv run pytest ...`

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `app/collectors/macro.py`（改） | 按真实表头解析 FRED CSV |
| `app/workers/pipeline.py`（改） | FRED 落库从 insert-only 改为 upsert |
| `app/services/context.py`（新） | 每轮从库里装载宏观事实与 FED 新闻 |
| `app/services/cycle.py`（改） | 注入 `macro_events`；K 线 limit 1000；微观结构落库；外部故障不熔断 |
| `app/domain/schemas.py`（改） | `MarketSnapshot` 扩 7 字段；新增 `MarketMicrostructure` |
| `app/exchange/base.py`（改） | `ExchangeClient` 协议加 `get_microstructure` |
| `app/exchange/weex.py`（改） | ticker 补字段；实现微观结构采集 |
| `app/exchange/fixtures.py`（改） | fixture 跟上 domain 变更 |
| `app/collectors/weex.py`（改） | 新增微观结构采集入口；candle 默认 limit |
| `app/db/models.py`（改） | `MarketSnapshot` 加列；新增 `MarketMicrostructureRecord` |
| `alembic/versions/006_market_snapshot_range_fields.py`（新） | 给已有表加列 |

---

## Task 1: 修 FRED 值列解析

**问题**：`app/collectors/macro.py:82` 读 `row.get("value") or row.get("VALUE")`，但 FRED 的真实表头是
`observation_date,<SERIES_ID>`（实测 `observation_date,CPIAUCSL`）。所以库里 152 行全部解析成 `NULL`。

**Files:**
- Modify: `app/collectors/macro.py`
- Test: `tests/unit/test_collectors.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/unit/test_collectors.py` 末尾（该文件已 import `MacroCollector`）：

```python
def test_fred_values_are_parsed_from_the_real_series_named_header() -> None:
    """FRED 的 CSV 表头是 `observation_date,<SERIES_ID>`，值列名就是序列 id。

    早期实现读的是 `row["value"]`，而 FRED 从来没有过这个列名 —— 结果每一条都
    解析成 None，宏观 agent 永远拿到空上下文。
    """
    payload = b"observation_date,CPIAUCSL\n2026-06-01,332.568\n2026-07-01,332.813\n"

    result = MacroCollector(fetcher=lambda _: payload).collect_fred(("CPIAUCSL",))

    assert [item.value for item in result.items] == [332.568, 332.813]
    assert [item.observation_date.isoformat() for item in result.items] == ["2026-06-01", "2026-07-01"]
    assert result.ok


def test_fred_missing_value_marker_becomes_none() -> None:
    """FRED 用 `.` 表示非交易日/未发布，必须映射成 None 而不是抛异常。"""
    payload = b"observation_date,DGS10\n2026-07-03,.\n2026-07-04,\n"

    result = MacroCollector(fetcher=lambda _: payload).collect_fred(("DGS10",))

    assert [item.value for item in result.items] == [None, None]
    assert result.ok


def test_fred_legacy_value_header_is_still_accepted() -> None:
    """旧式表头（列名就叫 value）要继续能解析。"""
    payload = b"observation_date,value\n2026-07-01,4.25\n"

    result = MacroCollector(fetcher=lambda _: payload).collect_fred(("DGS10",))

    assert [item.value for item in result.items] == [4.25]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/unit/test_collectors.py -k fred -q
```

Expected: 前两个 FAIL（值是 `[None, None]` 或抛 `KeyError`）。第三个可能碰巧通过 —— 记下实际输出再继续。

- [ ] **Step 3: 实现**

在 `app/collectors/macro.py` 的 `FRED_GRAPH_URL` 下面加：

```python
#: FRED 的 CSV 表头形如 `observation_date,CPIAUCSL` —— 值列名就是序列 id，
#: 不是固定的 "value"。早期实现按 "value" 取值，导致所有观测都解析成 None。
FRED_DATE_COLUMNS = ("observation_date", "DATE")
FRED_VALUE_COLUMNS = ("value", "VALUE")


def _fred_value_column(fieldnames: list[str] | None, series_id: str) -> str | None:
    """挑出承载数值的那一列，优先序列 id，其次兼容旧式 value/VALUE 表头。"""

    named = [name for name in (fieldnames or []) if name not in FRED_DATE_COLUMNS]
    if not named:
        return None
    for candidate in (series_id, *FRED_VALUE_COLUMNS):
        if candidate in named:
            return candidate
    return named[0]
```

把 `collect_fred` 的循环体改成：

```python
                reader = csv.DictReader(io.StringIO(text))
                value_column = _fred_value_column(reader.fieldnames, series_id)
                rows = list(reader)
                for row in rows[-limit:]:
                    raw_date = (row.get("observation_date") or row.get("DATE") or "").strip()
                    raw_value = ((row.get(value_column) if value_column else None) or "").strip()
                    result.items.append(
                        MacroObservation(
                            series_id=series_id,
                            observation_date=date.fromisoformat(raw_date),
                            value=None if raw_value in {"", "."} else float(raw_value),
                            source_url=url,
                            fetched_at=result.collected_at,
                        )
                    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/unit/test_collectors.py -q
```

Expected: 全部 PASS（原有 RSS / 失败隔离用例也不能挂）。

- [ ] **Step 5: 提交**

```bash
git add app/collectors/macro.py tests/unit/test_collectors.py
git commit -m "fix: read FRED values from the series-named column

FRED's CSV header is observation_date,<SERIES_ID>, not a fixed value
column, so every observation parsed to None — 152 rows in the database,
all NULL, which is why the macro agent always saw an empty context."
```

---

## Task 2: FRED 落库改成 upsert

**问题**：`app/workers/pipeline.py:76-92` 只在 `existing is None` 时插入。Task 1 修好解析后，
已存在的 152 行不会因为「行已存在」而更新 —— 会永远保持 NULL。

**不需要一次性回填脚本**：`SourceSchedule._last_fetch`（`app/workers/schedule.py:22`）是**进程内存**，
worker 重启后所有源都算「到期」，会立刻重抓。所以只要落库支持更新，重启 worker 即可自愈。

**Files:**
- Modify: `app/workers/pipeline.py:76-135`
- Test: `tests/integration/test_document_pipeline.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/integration/test_document_pipeline.py` 末尾（该文件已有 `FakeRSS` / `FakeSummary` /
`FakeEmbedder` / `FakeIndexer` 等桩件，直接复用）：

```python
class MutableMacro:
    """值可控的 FRED 桩件，用来验证同一观测被重复抓取时会不会更新。"""

    def __init__(self) -> None:
        self.value = 5.1

    def collect(self, series_ids=(), fred_limit=30, fed_feed_url=None):
        del series_ids, fred_limit, fed_feed_url
        return (
            CollectorResult(
                source="fred",
                items=[
                    MacroObservation(
                        series_id="DFF",
                        observation_date=datetime.now(UTC).date(),
                        value=self.value,
                        source_url="https://fred.example/DFF",
                        fetched_at=datetime.now(UTC),
                    )
                ],
                succeeded=["DFF"],
            ),
            CollectorResult(source="federal-reserve"),
        )


def test_republishing_a_macro_observation_updates_the_stored_value(tmp_path):
    """FRED 会修订历史值。落库只 insert 不 update 的话，首次写坏的值永远改不回来。

    这不是假想问题：解析 bug 期间写进去的 152 行全是 NULL，修好解析后仍然会
    因为「行已存在」而全部跳过。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'macro.db'}")
    Base.metadata.create_all(engine)
    macro = MutableMacro()

    with Session(engine) as db:
        # 参数名照该文件既有的 test_document_pipeline_marks_empty_content_as_skipped_not_indexed
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeEmptyRSS(),
            macro=macro,
            summary_client=FakeSummary(),
            embedder=FakeEmbedder(),
            indexer=FakeIndexer(),
        )

        pipeline.run_once()
        macro.value = 5.25
        pipeline.run_once()

        # 断言必须在 with 内：出去之后会话关闭，访问属性会 DetachedInstanceError
        rows = db.scalars(select(MacroObservationRecord)).all()
        assert len(rows) == 1, "同一 (series_id, observation_date) 不应重复插入"
        assert rows[0].value == 5.25, "修订后的值必须覆盖旧值"
```

> 两个细节：`FakeEmptyRSS` 的内容为空，会在摘要前被跳过，所以不会真的调 Ark / Milvus；
> `MutableMacro` 无视 `series_ids` 恒返回同一条观测，因此不需要操控 `clock`。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/integration/test_document_pipeline.py -k republishing -q
```

Expected: FAIL，`rows[0].value` 是 `5.1`。

- [ ] **Step 3: 实现**

`app/workers/pipeline.py` 的观测落库块改成 insert-or-update：

```python
        new_observations = 0
        updated_observations = 0
        for observation in observations.items:
            observation_date = datetime.combine(
                observation.observation_date,
                time.min,
                tzinfo=UTC,
            )
            existing = self.db.scalar(
                select(MacroObservationRecord).where(
                    MacroObservationRecord.series_id == observation.series_id,
                    MacroObservationRecord.observation_date == observation_date,
                )
            )
            if existing is None:
                self.db.add(
                    MacroObservationRecord(
                        series_id=observation.series_id,
                        observation_date=observation_date,
                        value=observation.value,
                        source_url=observation.source_url,
                        fetched_at=observation.fetched_at,
                    )
                )
                new_observations += 1
            elif existing.value != observation.value:
                # FRED 会修订历史值；解析曾经写坏的 NULL 也靠这条自愈。
                existing.value = observation.value
                existing.fetched_at = observation.fetched_at
                updated_observations += 1
```

同一方法里那条 `"collection: news=%d macro_obs=%d(+%d new) ..."` 日志补上 updated 计数。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/integration/test_document_pipeline.py -q
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/workers/pipeline.py tests/integration/test_document_pipeline.py
git commit -m "fix: update stored macro observations instead of skipping them

Persistence only inserted when the (series_id, observation_date) row was
missing, so a value written wrong once could never be corrected. FRED
revises history, and the parsing bug left 152 NULL rows that would have
stayed NULL forever. SourceSchedule lives in process memory, so a worker
restart refetches everything and self-heals."
```

---

## Task 2.5: 对账要把消失的仓位置为已平

**问题**：`app/reconciliation/service.py:153` 的 `_persist_db` 只遍历交易所**当前回报**的仓位，
对「本地有 OPEN 行、但交易所已无该仓位」不做任何处理。于是任何**不经过本系统 CLOSE 路径**的退出
（止损触发、交易所侧强平、人工平仓、别的工具下单）都会在本地留下永远 `OPEN` 的幽灵仓位。

**已实际发生**：在交易所手工开了一笔 BTC 空头，worker 恰好在那个对账窗口里把它写成 OPEN；
随后手工平掉，交易所返回空仓位，但本地那行至今停在 OPEN，账本页面把它当活仓位显示。

**同一条道理在内存那份已经处理过**（`reconcile()` 里的注释：「positions 是当前状态而不是事件流：
整体替换，否则已平掉的仓位会永远留在结果里」）—— 只有 DB 这一支漏了。

**影响不止于显示**：第 3 层的 PositionManager 靠仓位状态判断保本 / 移动止损，幽灵仓位会让它
对着一个不存在的仓位反复下 reduceOnly 单。

**Files:**
- Modify: `app/reconciliation/service.py:153-176`
- Test: `tests/integration/test_reconciliation_persistence.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/integration/test_reconciliation_persistence.py` 末尾（该文件已有 `_scoped_db` /
`_reconcile` / `NoTradeFeedFixture` / `_FlatFixture`，直接复用）：

```python
def test_reconciliation_closes_local_rows_when_the_exchange_position_is_gone(tmp_path) -> None:
    """本地仓位的 status 必须跟着交易所走，否则会留下幽灵持仓。

    实测触发：在交易所手工开了一笔空头，worker 恰好在那个对账窗口里把它写成 OPEN；
    随后手工平掉，交易所返回空仓位，本地那行却一直停在 OPEN —— 账本页面把它当活仓位
    显示，持仓管理也会对着不存在的仓位下单。
    """
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    _reconcile(service, NoTradeFeedFixture("1000"))
    db.commit()
    assert [row.status for row in db.scalars(select(Position)).all()] == ["OPEN"]

    _reconcile(service, _FlatFixture("1000"))
    db.commit()

    rows = db.scalars(select(Position)).all()
    assert rows != [], "行要保留供审计，只改状态"
    assert [row.status for row in rows] == ["CLOSED"]
    assert rows[0].quantity == Decimal(0), "已平仓位不该继续报告数量"


def test_reopening_the_same_symbol_marks_the_row_open_again(tmp_path) -> None:
    """唯一键是 (user, account, symbol)，平仓后重开会复用同一行，状态要能回头。"""
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    _reconcile(service, NoTradeFeedFixture("1000"))
    _reconcile(service, _FlatFixture("1000"))
    _reconcile(service, NoTradeFeedFixture("1000"))
    db.commit()

    rows = db.scalars(select(Position)).all()
    assert len(rows) == 1, "同一 symbol 不该产生第二行"
    assert rows[0].status == "OPEN"
    assert rows[0].quantity == Decimal("0.01")
```

> `Decimal` 与 `Position` 已在该文件的 import 里，无需新增。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/integration/test_reconciliation_persistence.py -k "closes_local_rows or reopening" -q
```

Expected: 第一个 FAIL（状态仍是 `["OPEN"]`）；第二个可能碰巧通过 —— 记下实际输出再继续。

- [ ] **Step 3: 实现**

在 `app/reconciliation/service.py` 的 `_persist_db` 里，把仓位循环改成：

```python
        for item_position in positions:
            position_row = db.scalar(
                select(Position).where(
                    Position.user_id == user_id,
                    Position.trading_account_id == trading_account_id,
                    Position.symbol == item_position.symbol,
                )
            )
            if position_row is None:
                position_row = Position(
                    id=str(uuid4()),
                    user_id=user_id,
                    trading_account_id=trading_account_id,
                    symbol=item_position.symbol,
                    side=item_position.side,
                    quantity=item_position.quantity,
                    entry_price=item_position.entry_value / item_position.quantity if item_position.quantity else 0,
                )
                db.add(position_row)
            position_row.side = item_position.side
            position_row.quantity = item_position.quantity
            position_row.mark_price = item_position.entry_value / item_position.quantity if item_position.quantity else None
            position_row.leverage = item_position.leverage
            position_row.unrealized_pnl = item_position.unrealized_pnl
            # 同一 symbol 平仓后重开会复用这行（唯一键是 user+account+symbol），
            # 所以每次观测到仓位都要把状态翻回 OPEN。
            position_row.status = "OPEN"

        # 交易所没回报的仓位必须在本地跟着平掉。positions 是当前状态而不是事件流 ——
        # 不做这步，任何不走本系统 CLOSE 路径的退出（止损触发、人工平仓、强平）都会
        # 在本地留下永远 OPEN 的幽灵仓位。
        reported_symbols = {item.symbol for item in positions}
        for stale_row in db.scalars(
            select(Position).where(
                Position.user_id == user_id,
                Position.trading_account_id == trading_account_id,
                Position.status == "OPEN",
            )
        ).all():
            if stale_row.symbol in reported_symbols:
                continue
            stale_row.status = "CLOSED"
            stale_row.quantity = Decimal(0)
            stale_row.unrealized_pnl = Decimal(0)
            stale_row.stop_loss = None
            stale_row.take_profit = None
```

> 这段必须放在仓位 upsert 循环**之后**。新加入的行此时还在 pending（会话是
> `autoflush=False`，这条 `select` 不会触发 flush），所以不会被误判成 stale —— 不要为了
> 「保险」去加 `db.flush()`，那样反而会把新行扫进来。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/integration/test_reconciliation_persistence.py -q
```

Expected: 全部 PASS（含原有的 `test_reconciliation_positions_reflect_current_state_not_history`）。

- [ ] **Step 5: 提交**

```bash
git add app/reconciliation/service.py tests/integration/test_reconciliation_persistence.py
git commit -m "fix: close local position rows the exchange no longer reports

_persist_db only walked the positions the exchange returned, so any exit
that does not go through this system's CLOSE path — a stop trigger, a
manual close, a liquidation — left the local row OPEN forever. The
in-memory store already handled this ('positions is current state, not an
event stream'); the DB branch did not. A phantom row shows up in the
ledger as a live position and would have had position management send
reduce-only orders against a position that does not exist."
```

---

## Task 3: 宏观上下文 loader

**问题**：`app/services/cycle.py:158` 构造 `TradingCycleState` 时没传 `macro_events`，
宏观 agent（`app/agents/graph.py:166-170`）的上下文恒为空数组 —— 19 条决策的 macro 分析
全部 `insufficient_data`、confidence 0.0。

**Files:**
- Create: `app/services/context.py`
- Modify: `app/services/cycle.py`
- Test: `tests/unit/test_macro_context.py`（新建）

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/test_macro_context.py`：

```python
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, MacroObservationRecord, SourceDocument
from app.services.context import load_macro_context


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ctx.db'}")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_macro_context_reports_latest_value_and_change(tmp_path):
    """宏观 agent 需要的是「现在多少、比上期变了多少」，不是原始行列表。"""
    with _session(tmp_path) as db:
        base = datetime(2026, 7, 1, tzinfo=UTC)
        for offset, value in ((0, 332.5), (1, 332.8)):
            db.add(MacroObservationRecord(
                series_id="CPIAUCSL",
                observation_date=base + timedelta(days=30 * offset),
                value=value,
                source_url="https://fred.example/CPIAUCSL",
                fetched_at=base,
            ))
        db.commit()

        context = load_macro_context(db, limit=10)

    series = {item["series_id"]: item for item in context if item.get("kind") == "macro_series"}
    assert series["CPIAUCSL"]["value"] == 332.8
    assert series["CPIAUCSL"]["previous_value"] == 332.5
    assert round(series["CPIAUCSL"]["change"], 4) == 0.3


def test_macro_context_skips_null_values(tmp_path):
    """解析修好之前写进库的 NULL 行不能污染上下文。"""
    with _session(tmp_path) as db:
        db.add(MacroObservationRecord(
            series_id="DGS10",
            observation_date=datetime(2026, 7, 1, tzinfo=UTC),
            value=None,
            source_url="https://fred.example/DGS10",
            fetched_at=datetime.now(UTC),
        ))
        db.commit()

        assert load_macro_context(db, limit=10) == []


def test_macro_context_includes_fed_press_documents(tmp_path):
    """FED 官方新闻走的是 source_documents，不是 macro_observations。"""
    with _session(tmp_path) as db:
        db.add(SourceDocument(
            id="doc-1",
            source="federal-reserve",
            canonical_url="https://fed.example/1",
            title="Fed holds rates",
            raw_text="text",
            cleaned_text="text",
            content_hash="hash-1",
            language="en",
            published_at=datetime.now(UTC) - timedelta(hours=2),
            fetched_at=datetime.now(UTC),
            processing_status="INDEXED",
        ))
        db.commit()

        context = load_macro_context(db, limit=10)

    press = [item for item in context if item.get("kind") == "fed_press"]
    assert len(press) == 1
    assert press[0]["title"] == "Fed holds rates"


def test_macro_context_is_empty_when_nothing_is_stored(tmp_path):
    with _session(tmp_path) as db:
        assert load_macro_context(db, limit=10) == []


def test_macro_context_tolerates_a_missing_session():
    """库不可用时返回空列表：宏观缺失只该让 agent 说「数据不足」，不该让周期崩。"""
    assert load_macro_context(None, limit=10) == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/unit/test_macro_context.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.context'`。

- [ ] **Step 3: 实现**

新建 `app/services/context.py`：

```python
"""把数据库里已有的宏观事实装配成决策周期可用的上下文。

行情事实不写进 RAG（见 CLAUDE.md），所以宏观数值只能从库里读。周期本身不做采集 ——
采集由 DocumentPipeline 负责，这里只负责「读出来、算成 agent 能用的形状」。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MacroObservationRecord, SourceDocument

#: FED 新闻只取最近这么多天的，更早的对短线决策没有意义。
FED_PRESS_WINDOW_DAYS = 7
#: 每个序列最多取两条观测，用来算变化。
SERIES_HISTORY = 2


def _series_context(db: Session) -> list[dict[str, Any]]:
    """每个 series 取最近两个非空观测，给出当前值、前值与变化。"""

    rows = db.scalars(
        select(MacroObservationRecord)
        .where(MacroObservationRecord.value.is_not(None))
        .order_by(
            MacroObservationRecord.series_id,
            MacroObservationRecord.observation_date.desc(),
        )
    ).all()

    buckets: dict[str, list[MacroObservationRecord]] = {}
    for row in rows:
        bucket = buckets.setdefault(row.series_id, [])
        if len(bucket) < SERIES_HISTORY:
            bucket.append(row)

    context: list[dict[str, Any]] = []
    for series_id, bucket in sorted(buckets.items()):
        current = bucket[0]
        entry: dict[str, Any] = {
            "kind": "macro_series",
            "series_id": series_id,
            "observation_date": current.observation_date.date().isoformat(),
            "value": float(current.value),
        }
        previous = bucket[1] if len(bucket) > 1 else None
        if previous is not None and previous.value is not None:
            entry["previous_value"] = float(previous.value)
            entry["change"] = float(current.value) - float(previous.value)
        context.append(entry)
    return context


def _fed_press_context(db: Session, *, now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(days=FED_PRESS_WINDOW_DAYS)
    rows = db.scalars(
        select(SourceDocument)
        .where(
            SourceDocument.source == "federal-reserve",
            SourceDocument.published_at.is_not(None),
            SourceDocument.published_at >= since,
        )
        .order_by(SourceDocument.published_at.desc())
        .limit(5)
    ).all()
    return [
        {
            "kind": "fed_press",
            "title": row.title,
            "published_at": row.published_at.isoformat(),
            "source_url": row.canonical_url,
        }
        for row in rows
    ]


def load_macro_context(
    db: Session | None,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """返回可直接放进 ``TradingCycleState.macro_events`` 的列表。"""

    if db is None:
        return []
    context = [*_series_context(db), *_fed_press_context(db, now=now or datetime.now(UTC))]
    return context[:limit]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/unit/test_macro_context.py -q
```

Expected: 全部 PASS。

- [ ] **Step 5: 接进决策周期**

`app/services/cycle.py` 顶部 import 加：

```python
from app.services.context import load_macro_context
```

把 `TradingCycleState(...)`（约 158 行）的构造补上：

```python
            errors=[*candle_result.errors, *snapshot_result.errors],
            macro_events=load_macro_context(self.db, limit=20),
```

- [ ] **Step 6: 跑全量测试**

```bash
cd backend && uv run pytest -q
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add app/services/context.py app/services/cycle.py tests/unit/test_macro_context.py
git commit -m "feat: feed macro observations and Fed press into the cycle state

TradingCycleState was built without macro_events, so the macro agent's
context was always an empty list and every decision recorded
insufficient_data with confidence 0.0. Load stored observations as
latest-value-plus-change, plus recent Fed press documents, which live in
source_documents rather than the macro table."
```

---

## Task 4: ticker 补齐被丢弃的 7 个字段

**问题**：`app/exchange/weex.py:245-272` 只读了 `lastPrice` 和 `volume`。实测 ticker 还返回
`openPrice` / `highPrice` / `lowPrice` / `priceChangePercent` / `quoteVolume` / `markPrice` / `indexPrice`，
全部被丢弃。

**Files:**
- Modify: `app/domain/schemas.py:26-35`
- Modify: `app/exchange/weex.py:245-272`
- Modify: `app/exchange/fixtures.py:34-41`
- Test: `tests/contract/test_weex.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/contract/test_weex.py` 末尾（该文件已有 `_client_for(handler)` 与
`TICKER_RESPONSE` 可直接用）：

```python
def test_ticker_range_and_basis_fields_are_kept() -> None:
    """24h ticker 返回 11 个字段，早期实现只读了 lastPrice 和 volume。

    丢掉 openPrice/highPrice/lowPrice 让 market agent 无法判断价格在日内区间
    的位置；丢掉 markPrice/indexPrice 让它看不到基差。
    """
    ticker = {
        **TICKER_RESPONSE,
        "openPrice": "78181.4",
        "highPrice": "78496.1",
        "lowPrice": "76414.5",
        "priceChangePercent": "-0.012630",
        "quoteVolume": "1817917050.88401",
        "markPrice": "77199.2",
        "indexPrice": "77237.45275",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capi/v3/market/ticker/24hr":
            return httpx.Response(200, json=[ticker])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler) as client:
        snapshot = client.get_market_snapshot("BTC-USDT")

    assert snapshot.last_price == 1.5
    assert snapshot.open_24h == 78181.4
    assert snapshot.high_24h == 78496.1
    assert snapshot.low_24h == 76414.5
    assert snapshot.price_change_pct == -0.012630
    assert snapshot.quote_volume_24h == 1817917050.88401
    assert snapshot.mark_price == 77199.2
    assert snapshot.index_price == 77237.45275
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/contract/test_weex.py -k ticker_range -q
```

Expected: FAIL，`AttributeError: 'MarketSnapshot' object has no attribute 'open_24h'`。

- [ ] **Step 3: 扩展 domain 模型**

`app/domain/schemas.py` 的 `MarketSnapshot` 改成：

```python
class MarketSnapshot(BaseModel):
    symbol: str
    captured_at: int
    last_price: float
    #: 24h 区间与量能。WEEX 的 ticker 全部返回，早期实现丢掉了。
    open_24h: float | None = None
    high_24h: float | None = None
    low_24h: float | None = None
    price_change_pct: float | None = None
    quote_volume_24h: float | None = None
    #: 标记价与指数价，用来算基差。
    mark_price: float | None = None
    index_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    volume_24h: float | None = None
    source: str = "weex"
```

- [ ] **Step 4: 扩展 WEEX 解析**

`app/exchange/weex.py` 的 `get_market_snapshot` 里，在 `return MarketSnapshot(` 之前加取值辅助：

```python
        def optional(name: str) -> float | None:
            raw_value = ticker.get(name)
            return float(_decimal(raw_value)) if raw_value is not None else None
```

并把构造改成（注意保留原有的 `captured_at = self._clock_ms()` 与它的注释）：

```python
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
```

- [ ] **Step 5: 同步 fixture**

`app/exchange/fixtures.py` 的 `TICKER_RESPONSE` 补上新字段（数值自拟，保持与既有 `lastPrice: "1.5"` 量级一致）：

```python
TICKER_RESPONSE = {
    "symbol": "BTCUSDT",
    "lastPrice": "1.5",
    "bidPrice": "1.4",
    "askPrice": "1.6",
    "volume": "100",
    "quoteVolume": "150",
    "openPrice": "1.4",
    "highPrice": "1.7",
    "lowPrice": "1.3",
    "priceChangePercent": "0.0714",
    "markPrice": "1.5",
    "indexPrice": "1.51",
    "closeTime": 1700000299999,
}
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/contract/test_weex.py tests/unit -q
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add app/domain/schemas.py app/exchange/weex.py app/exchange/fixtures.py tests/contract/test_weex.py
git commit -m "feat: keep the seven ticker fields the collector was dropping

WEEX's 24h ticker returns open/high/low/priceChangePercent/quoteVolume/
markPrice/indexPrice alongside lastPrice; only lastPrice and volume were
read. Without the 24h range the market agent cannot say where price sits
in the day's band, and without mark/index it cannot see the basis."
```

---

## Task 5: 迁移 006 —— 给 market_snapshots 加列

**问题**：`open_24h` / `high_24h` / `low_24h` / `price_change_pct` / `quote_volume_24h` /
`mark_price` / `index_price` 需要落库，但 `market_snapshots` 没有这些列。
按 `CLAUDE.md` 的约定：`001` 的 `create_all(checkfirst=True)` 只补建缺失的表，
**给已存在的表加列必须写显式增量迁移**。当前 head 是 `005_user_locale`。

**Files:**
- Modify: `app/db/models.py:131-145`
- Create: `alembic/versions/006_market_snapshot_range_fields.py`
- Modify: `app/services/cycle.py:618-629`
- Test: `tests/integration/test_migrations.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/integration/test_migrations.py` 末尾（该文件已有 `_upgrade(url)` 与
`_columns(url, table)` 两个辅助，直接复用）：

```python
def test_market_snapshots_gains_the_range_and_basis_columns(tmp_path) -> None:
    """新增列必须由增量迁移承载 —— create_all 不会动已存在的表。

    market_snapshots 在 001 里就已经建出来了，所以这几列只能走显式 op.add_column。
    """
    url = f"sqlite+pysqlite:///{tmp_path / 'range.db'}"

    _upgrade(url)

    columns = _columns(url, "market_snapshots")
    for name in (
        "open_24h",
        "high_24h",
        "low_24h",
        "price_change_pct",
        "quote_volume_24h",
        "mark_price",
        "index_price",
    ):
        assert name in columns, f"market_snapshots 缺少 {name}"


def test_market_snapshot_migration_is_repeatable(tmp_path) -> None:
    """迁移要能对已经加过列的库重复执行（照 002 的模式）。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'rerun.db'}"

    _upgrade(url)
    _upgrade(url)

    assert "high_24h" in _columns(url, "market_snapshots")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/integration/test_migrations.py -k range_and_basis -q
```

Expected: FAIL，`market_snapshots 缺少 open_24h`。

- [ ] **Step 3: 改模型**

`app/db/models.py` 的 `MarketSnapshot`（131 行起）在 `last_price` 之后插入：

```python
    open_24h: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    high_24h: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    low_24h: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    price_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    index_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
```

- [ ] **Step 4: 写增量迁移**

新建 `alembic/versions/006_market_snapshot_range_fields.py`：

```python
"""add market_snapshots 24h range and basis columns

001 只按当前模型补建缺失的表，不动已存在的表，所以给 market_snapshots 加列
必须在这里显式承载。写法对 PostgreSQL 与 SQLite 都成立，且可重复执行。
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "006_market_snapshot_range_fields"
down_revision = "005_user_locale"
branch_labels = None
depends_on = None

TABLE = "market_snapshots"
COLUMNS = (
    ("open_24h", sa.Numeric(30, 12)),
    ("high_24h", sa.Numeric(30, 12)),
    ("low_24h", sa.Numeric(30, 12)),
    ("price_change_pct", sa.Float()),
    ("quote_volume_24h", sa.Float()),
    ("mark_price", sa.Numeric(30, 12)),
    ("index_price", sa.Numeric(30, 12)),
)


def _present() -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    # 空库由 001 直接建出带这些列的表，这里要能跳过。
    present = _present()
    for name, type_ in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    present = _present()
    for name, _ in COLUMNS:
        if name in present:
            op.drop_column(TABLE, name)
```

- [ ] **Step 5: 落库时写入新列**

`app/services/cycle.py` 的 `_persist_market_data` 里，`MarketSnapshotModel(...)` 构造补上：

```python
                    open_24h=snapshot.open_24h,
                    high_24h=snapshot.high_24h,
                    low_24h=snapshot.low_24h,
                    price_change_pct=snapshot.price_change_pct,
                    quote_volume_24h=snapshot.quote_volume_24h,
                    mark_price=snapshot.mark_price,
                    index_price=snapshot.index_price,
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/integration/test_migrations.py -q
```

Expected: 全部 PASS（含原有的「已有数据的库也能升级」用例）。

- [ ] **Step 7: 提交**

```bash
git add app/db/models.py alembic/versions/006_market_snapshot_range_fields.py app/services/cycle.py tests/integration/test_migrations.py
git commit -m "feat: persist the 24h range and basis fields on market snapshots

Adds an incremental migration rather than relying on create_all, which
only creates missing tables and never touches an existing one."
```

---

## Task 6: `MarketMicrostructure` —— 盘口、订单流、资金费率、持仓量

**问题**：`depth` / `trades` / `fundingRate` / `openInterest` 都可用（见 `docs/weex-virtual-api.md`），
但目前一个都没采。这些字段**单独建模型**：它们的失败模式与 ticker 不同，「ticker 成功但 depth
失败」是常态，混在一个模型里就分不清「没采到」和「采到但是空」。

> ⚠️ 实测显示这些数值**很可能是合成的**（`depth` 价差 0.00013%）。本层只负责**采集与存储**，
> 不参与任何决策 —— 打分是否使用它们由第 2 层决定。

**Files:**
- Modify: `app/domain/schemas.py`（新增 `MarketMicrostructure`）
- Modify: `app/exchange/base.py`（协议加方法）
- Modify: `app/exchange/weex.py`（实现）
- Modify: `app/collectors/weex.py`（采集入口）
- Modify: `app/services/cycle.py`（采集 + 落库）
- Modify: `app/db/models.py`（新增 `MarketMicrostructureRecord`）
- Modify: `app/exchange/fixtures.py`（新增 fixture）
- Test: `tests/contract/test_weex.py`、`tests/api/test_cycle_service.py`

- [ ] **Step 1: 写失败的测试**

先给 `app/exchange/fixtures.py` 加 fixture：

```python
DEPTH_RESPONSE = {
    "asks": [["77230.0", "1.0"], ["77230.2", "1.0"]],
    "bids": [["77229.9", "3.0"], ["77229.7", "1.0"]],
}

TRADES_RESPONSE = [
    {"price": "77229.9", "qty": "1.0", "isBuyerMaker": False},
    {"price": "77229.9", "qty": "1.0", "isBuyerMaker": True},
    {"price": "77229.9", "qty": "2.0", "isBuyerMaker": False},
]

FUNDING_RATE_RESPONSE = [{"fundingRate": "0.00003006", "fundingTime": 1789084800000}]

OPEN_INTEREST_RESPONSE = {"openInterest": "140652.2160", "time": 1789109251138}
```

追加到 `tests/contract/test_weex.py`（import 处补上这四个 fixture）：

```python
def test_microstructure_derives_spread_imbalance_and_taker_ratio() -> None:
    """盘口与成交流水要压成三个可比较的数，而不是把原始数组塞进状态。"""

    def handler(request: httpx.Request) -> httpx.Response:
        routes = {
            "/capi/v3/market/depth": DEPTH_RESPONSE,
            "/capi/v3/market/trades": TRADES_RESPONSE,
            "/capi/v3/market/fundingRate": FUNDING_RATE_RESPONSE,
            "/capi/v3/market/openInterest": OPEN_INTEREST_RESPONSE,
        }
        if request.url.path in routes:
            return httpx.Response(200, json=routes[request.url.path])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with _client_for(handler) as client:
        micro = client.get_microstructure("BTC-USDT")

    assert micro.bid == 77229.9
    assert micro.ask == 77230.0
    # (买量 4.0 - 卖量 2.0) / (4.0 + 2.0)
    assert round(micro.depth_imbalance, 6) == round(2.0 / 6.0, 6)
    # 主动买 3.0 / 总量 4.0
    assert micro.taker_buy_ratio == 0.75
    assert micro.funding_rate == 0.00003006
    assert micro.open_interest == 140652.2160
    assert micro.spread_bps is not None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/contract/test_weex.py -k microstructure -q
```

Expected: FAIL，`AttributeError: ... has no attribute 'get_microstructure'`。

- [ ] **Step 3: 加 domain 模型**

`app/domain/schemas.py` 在 `MarketSnapshot` 之后加：

```python
class MarketMicrostructure(BaseModel):
    """一次观测的盘口 / 订单流 / 衍生品快照。

    与 ``MarketSnapshot`` 分开建模，因为失败模式不同 —— ticker 成功而 depth 失败
    是常态，合在一起就分不清「没采到」与「采到但是空」。

    ⚠️ 虚拟盘的这些数值疑似合成数据（实测价差低到 0.00013%），只可作辅助确认项。
    """

    symbol: str
    captured_at: int
    bid: float | None = None
    ask: float | None = None
    spread_bps: float | None = None
    #: (买量 - 卖量) / (买量 + 卖量)，取前 5 档。
    depth_imbalance: float | None = None
    #: 主动买量 / 总成交量。
    taker_buy_ratio: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    source: str = "weex"
```

- [ ] **Step 4: 实现采集**

`app/exchange/base.py` 的 `ExchangeClient` 协议里加一行（顶部 import 补 `MarketMicrostructure`）：

```python
    def get_microstructure(self, symbol: str) -> MarketMicrostructure: ...
```

`app/exchange/weex.py` 加方法：

```python
    def get_microstructure(self, symbol: str) -> MarketMicrostructure:
        """盘口 / 成交流水 / 资金费率 / 持仓量。

        虚拟盘上这四个端点都可用（见 docs/weex-virtual-api.md）。symbol 必须是不带
        横杠的合约符号，否则返回 -1142。
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
```

- [ ] **Step 5: 加采集入口**

`app/collectors/weex.py` 加方法（照 `collect_snapshots` 的失败隔离风格）：

```python
    def collect_microstructures(
        self,
        symbols: tuple[str, ...] = ("BTC-USDT", "ETH-USDT"),
    ) -> CollectorResult[MarketMicrostructure]:
        result: CollectorResult[MarketMicrostructure] = CollectorResult(source="weex-microstructure")
        for symbol in symbols:
            try:
                result.items.append(self.client.get_microstructure(symbol))
            except Exception as exc:  # noqa: BLE001 - isolate failures per market
                result.errors.append(f"{symbol}: {exc}")
        return result
```

顶部 import 补 `MarketMicrostructure`。

- [ ] **Step 6: 落库模型**

`app/db/models.py` 在 `MarketSnapshot` 之后加（**新表只改 models，不写迁移** ——
`001` 的 `create_all(checkfirst=True)` 会自动补建）：

```python
class MarketMicrostructureRecord(Base):
    __tablename__ = "market_microstructures"
    __table_args__ = (Index("ix_market_microstructures_lookup", "symbol", "captured_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_imbalance: Mapped[float | None] = mapped_column(Float, nullable=True)
    taker_buy_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
```

在 `tests/integration/test_migrations.py` 里加一个断言（复用该文件的 `_upgrade` 辅助）：

```python
def test_microstructure_table_is_created_by_the_initial_migration(tmp_path) -> None:
    """新表不需要写 op.create_table —— 001 的 create_all(checkfirst=True) 会补建。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'micro.db'}"

    _upgrade(url)

    assert "market_microstructures" in inspect(create_engine(url)).get_table_names()
```

- [ ] **Step 7: 接进决策周期**

`app/services/cycle.py` 的 `run()` 里，在 `candle_result, snapshot_result = collector.collect(...)`
之后加：

```python
        # 微观结构在虚拟盘上疑似合成数据，且失败模式独立于 ticker：采集失败只记错误，
        # 不影响本轮决策（第 1 层只负责把它存下来）。
        microstructure_result = collector.collect_microstructures(symbols=(symbol,))
```

把它的错误并入 state：

```python
            errors=[
                *candle_result.errors,
                *snapshot_result.errors,
                *microstructure_result.errors,
            ],
```

在 `self._persist_market_data(candle_result.items, snapshot_result.items)` 调用处补上第三个参数，
并在 `_persist_market_data` 签名与循环里落库：

```python
    def _persist_market_data(
        self,
        candles: list[Candle],
        snapshots: list[MarketSnapshot],
        microstructures: list[MarketMicrostructure] | None = None,
    ) -> None:
```

```python
        for micro in microstructures or []:
            self.db.add(
                MarketMicrostructureRecord(
                    symbol=micro.symbol,
                    captured_at=datetime.fromtimestamp(micro.captured_at / 1000, tz=UTC),
                    bid=micro.bid,
                    ask=micro.ask,
                    spread_bps=micro.spread_bps,
                    depth_imbalance=micro.depth_imbalance,
                    taker_buy_ratio=micro.taker_buy_ratio,
                    funding_rate=micro.funding_rate,
                    open_interest=micro.open_interest,
                )
            )
```

- [ ] **Step 8: 让测试桩件跟上**

`tests/api/test_cycle_service.py` 的 `FakeExchange` 加方法，否则 cycle 调用会炸：

```python
    def get_microstructure(self, symbol: str) -> MarketMicrostructure:
        return MarketMicrostructure(
            symbol=symbol,
            captured_at=int(datetime.now(UTC).timestamp() * 1000),
            bid=99.9,
            ask=100.1,
            spread_bps=20.0,
            depth_imbalance=0.1,
            taker_buy_ratio=0.55,
            funding_rate=0.0001,
            open_interest=1000.0,
        )
```

（import 处补 `MarketMicrostructure`。）同样检查 `app/exchange/fixtures.py` 里若有无微观结构的
替身类，一并补上。

再追加一个断言落库的测试：

```python
def test_cycle_persists_microstructure_without_affecting_the_decision(tmp_path) -> None:
    """微观结构只存不用：它的存在不应改变决策结果。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'micro.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = TradingCycleService(db=db, exchange_factory=FakeExchange)
        service.run(user_id="u-1", llm=FailingLLM())

        # 断言在 with 内：出去之后会话关闭，访问属性会 DetachedInstanceError
        rows = db.scalars(select(MarketMicrostructureRecord)).all()
        assert len(rows) == 1
        assert rows[0].symbol == "BTC-USDT"
```

- [ ] **Step 9: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/contract/test_weex.py tests/api/test_cycle_service.py tests/integration/test_migrations.py -q
```

Expected: 全部 PASS。

- [ ] **Step 10: 提交**

```bash
git add app/domain/schemas.py app/exchange/ app/collectors/weex.py app/services/cycle.py app/db/models.py tests/
git commit -m "feat: collect order book, tape, funding and open interest

Depth, trades, fundingRate and openInterest are all reachable on the
virtual account — the earlier 'no derivatives data' conclusion came from
reading the docs instead of probing (400 means the path exists, 404 means
it does not). Stored in a table of its own because it fails independently
of the ticker. Nothing consumes it yet: the values look synthetic, so the
scoring layer decides later whether to trust them."
```

---

## Task 7: K 线抓 1000 根

**问题**：`app/services/cycle.py:144-148` 传 `limit=100`，库里 1h 只有 102 根（4.25 天）。
实测 `klines` 单请求上限 **1000 根**（1h ≈ 41 天），且 `startTime`/`endTime` 被静默忽略 ——
没有分页可拉，1000 就是天花板。

**Files:**
- Modify: `app/services/cycle.py:144-148`
- Modify: `app/collectors/weex.py`
- Test: `tests/api/test_cycle_service.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/api/test_cycle_service.py`（复用该文件已有的 `FakeExchange` / `FailingLLM`）：

```python
class RecordingCandleExchange(FakeExchange):
    """记录每轮请求的 K 线深度，用来断言采集没有退回浅历史。"""

    def __init__(self) -> None:
        self.candle_limits: list[int] = []

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        self.candle_limits.append(limit)
        return super().get_candles(symbol, timeframe, limit)


def test_cycle_collects_the_deepest_history_the_api_allows() -> None:
    """历史深度直接决定能否回测。klines 无分页，单请求 1000 根就是上限。"""
    exchange = RecordingCandleExchange()
    service = TradingCycleService(exchange_factory=lambda: exchange)

    service.run(user_id="u-1", symbol="BTC-USDT", llm=FailingLLM())

    assert exchange.candle_limits == [1000, 1000, 1000], f"实际收到 {exchange.candle_limits}"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/api/test_cycle_service.py -k deepest_history -q
```

Expected: FAIL，`实际收到 [100, 100, 100]`。

- [ ] **Step 3: 实现**

`app/services/cycle.py` 的采集调用：

```python
        candle_result, snapshot_result = collector.collect(
            symbols=(symbol,),
            timeframes=("5m", "1h", "4h"),
            # WEEX 的 klines 无分页且忽略 startTime/endTime，单请求 1000 根就是历史
            # 天花板。1000 根 1h ≈ 41 天，是回测能拿到的最深样本。
            limit=1000,
        )
```

同时把 `app/collectors/weex.py` 里 `collect_candles` 与 `collect` 的 `limit` 默认值从 `100`
改成 `1000`，避免别处调用时又退回浅历史。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/api/test_cycle_service.py tests/unit/test_collectors.py -q
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/cycle.py app/collectors/weex.py tests/api/test_cycle_service.py
git commit -m "feat: collect 1000 candles per timeframe, the API's hard ceiling

The cycle asked for 100, so 1h history was 4.25 days — not enough to
backtest anything. klines allows 1000 per request and silently ignores
startTime/endTime, so there is no pagination to go deeper with."
```

---

## Task 8: 外部故障不再熔断账户

**问题**：`app/services/cycle.py:692` 的 `record_failure` 把所有周期失败都写成 `CYCLE_FAILURE`，
`_consecutive_failures` 按它计数，达到 `max_consecutive_failures`（默认 5）就调用 `_halt`
把账户 PAUSED。而 WEEX 的 503 是常规抖动 —— 今天已因此 PAUSED 两次，需要人工恢复。

**Files:**
- Modify: `app/services/cycle.py:692-735`
- Test: `tests/api/test_cycle_service.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/api/test_cycle_service.py`：

```python
def test_exchange_outage_does_not_pause_the_account(tmp_path) -> None:
    """对端 503 不是策略失败，不该把账户熔断。

    实测 WEEX 的 503 是间歇性的常规抖动（order/history、position/allPosition 都
    出现过）。把它算进连亏熔断，会让一次对端抖动把账户 PAUSED 到需要人工恢复。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'outage.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        service = TradingCycleService(db=db, settings=Settings(max_consecutive_failures=2))

        for _ in range(5):
            service.record_failure("u-1", "BTC-USDT", ExchangeError("WEEX request failed: 503"))

        assert service.control_status("u-1") == "RUNNING", "对端故障不应触发 pause"


def test_strategy_failures_still_pause_the_account(tmp_path) -> None:
    """外部故障豁免不能把真正的熔断也豁免掉。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'strategy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        service = TradingCycleService(db=db, settings=Settings(max_consecutive_failures=2))

        for _ in range(3):
            service.record_failure("u-1", "BTC-USDT", RuntimeError("committee output exploded"))

        assert service.control_status("u-1") == "PAUSED"
```

（import 处补 `ExchangeError`。）

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest tests/api/test_cycle_service.py -k pause_the_account -q
```

Expected: 第一个 FAIL（账户被 PAUSED），第二个 PASS。

- [ ] **Step 3: 实现**

`app/services/cycle.py` 顶部 import 加：

```python
import httpx

from app.exchange.base import ExchangeError
```

在 `RiskEvent` 相关的位置加一个模块级判定：

```python
#: 对端不可用（5xx / 超时 / 网络抖动）不是策略失败。把它算进连亏熔断会让一次
#: 短暂的对端抖动把账户 PAUSED 到需要人工恢复 —— 实测 WEEX 的 503 是常规抖动。
EXTERNAL_FAILURE_TYPES = (ExchangeError, httpx.TimeoutException, httpx.TransportError)


def is_external_failure(error: BaseException) -> bool:
    """httpx 的异常常被包在 ExchangeError 里，所以也要看 __cause__。"""

    candidates = (error, error.__cause__)
    return any(isinstance(item, EXTERNAL_FAILURE_TYPES) for item in candidates if item is not None)
```

把 `record_failure` 改成：

```python
    def record_failure(self, user_id: str, symbol: str, error: Exception) -> None:
        if self.db is None:
            return
        external = is_external_failure(error)
        self.db.add(
            RiskEvent(
                id=str(uuid4()),
                user_id=user_id,
                event_type="DATA_SOURCE_DEGRADED" if external else "CYCLE_FAILURE",
                status=RiskStatus.REJECTED,
                reason=str(error),
                metadata_json={"symbol": symbol, "external": external},
            )
        )
        self.db.commit()
        if external:
            # 只留痕，不动账户状态：下一轮对端恢复就自动继续。
            logger.warning("external data source failure, not counting toward the breaker: %s", error)
            return
        if self._consecutive_failures(user_id) >= self.settings.max_consecutive_failures:
            self._halt(
                user_id,
                RiskDecision(
                    status=RiskStatus.PAUSED, reasons=["repeated_cycle_failures"], halt=True
                ),
            )
```

> `_consecutive_failures` 只会数 `CYCLE_FAILURE`（`app/services/cycle.py:730`），
> 所以外部故障天然不进计数 —— **不要**去改 `_consecutive_failures` 的查询条件。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest tests/api/test_cycle_service.py -q
```

Expected: 全部 PASS（原有的熔断用例也必须通过）。

- [ ] **Step 5: 提交**

```bash
git add app/services/cycle.py tests/api/test_cycle_service.py
git commit -m "fix: stop treating exchange outages as strategy failures

Consecutive WEEX 5xx counted toward max_consecutive_failures and tripped
repeated_cycle_failures twice, pausing the account until a human resumed
it. External failures now record DATA_SOURCE_DEGRADED and leave the
account alone; only strategy failures feed the breaker."
```

---

## Task 9: 端到端校验

- [ ] **Step 1: 全量测试与静态检查**

```bash
cd backend && uv run pytest -q && uv run ruff check app tests workers && uv run mypy app
```

Expected: 全绿。

- [ ] **Step 2: 重启 worker，跑满一个采集周期**

```bash
cd backend && uv run python -m workers.scheduler
```

（`SourceSchedule` 是进程内存，重启即让所有源立刻重抓 —— 这是 Task 2 自愈的前提。）

- [ ] **Step 3: 核对数据**

```bash
cd backend && uv run python - <<'EOF'
from sqlalchemy import text
from app.db.session import SessionLocal

with SessionLocal() as db:
    print("macro:", db.execute(text(
        "SELECT series_id, count(*) AS n, count(value) AS non_null "
        "FROM macro_observations GROUP BY 1 ORDER BY 1"
    )).all())
    print("snapshots:", db.execute(text(
        "SELECT count(*), count(high_24h), count(mark_price) FROM market_snapshots"
    )).all())
    print("candles:", db.execute(text(
        "SELECT timeframe, count(*) FROM market_candles GROUP BY 1"
    )).all())
    print("micro:", db.execute(text(
        "SELECT count(*), count(funding_rate) FROM market_microstructures"
    )).all())
EOF
```

Expected：
- `macro_observations` 每行的 `non_null` 接近 `n`
- 最新的 `market_snapshots` 行 `high_24h` / `mark_price` 非空
- `market_candles` 的 1h 行数从 102 涨到约 1000
- `market_microstructures` 开始有行且 `funding_rate` 非空

- [ ] **Step 4: 确认宏观分析不再 `insufficient_data`**

```bash
cd backend && uv run python - <<'EOF'
from sqlalchemy import text
from app.db.session import SessionLocal

with SessionLocal() as db:
    for row in db.execute(text(
        "SELECT created_at, symbol, analyses->'macro'->>'status', "
        "analyses->'macro'->>'confidence' FROM trading_decisions "
        "ORDER BY created_at DESC LIMIT 5"
    )).all():
        print(row)
EOF
```

Expected: macro 的 `status` 不再是 `insufficient_data`，`confidence` 不再恒为 `0.0`。

> ⚠️ 本层只保证**原料到位**。决策是否从 HOLD 变成开仓取决于第 2 层 —— 这里看到 HOLD 是正常的，
> 不要为了让订单出现而放宽第 2 层以外的东西。

- [ ] **Step 5: 收尾提交**

```bash
git status --short
```

若 Step 1–4 一次通过、没有额外修改，则无需提交。

---

## 完成标准

- [ ] `macro_observations.value` 全表非空
- [ ] 决策记录里 macro 分析不再是 `insufficient_data`、confidence 不再是 0.0
- [ ] 新落的 `market_snapshots` 带 24h 区间与 mark/index
- [ ] `market_candles` 1h 深度达到约 1000 根
- [ ] `market_microstructures` 有数据（本层只存不用）
- [ ] 交易所没回报的仓位在本地被标为 `CLOSED`，账本页面不再显示幽灵持仓
- [ ] WEEX 连续 5xx 不再把账户 PAUSED，而策略性失败仍然熔断
- [ ] `uv run pytest` / `ruff check` / `mypy` 全绿

## 下一层

第 2 层（规则信号器 + LLM 封闭否决）的计划待本层落地后再写 —— 它要基于本层实际采集到的
字段与取值范围来定打分权重，提前写会变成猜测。

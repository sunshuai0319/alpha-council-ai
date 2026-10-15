"""REST 兜底对账：补 symbol、标记漏单。

能补的只有 symbol；漏掉的成交带来的盈亏补不回来（REST 没有 realizePnl），
所以漏单只能标记。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import Base, CollectorError, ExchangeFillRecord, ExchangeOrderEvent
from app.exchange.base import ExchangeOrder
from app.streams.protocol import FillEvent, OrderEvent
from app.streams.reconciler import GAP_COLLECTOR, OrderHistoryReconciler
from app.streams.repository import StreamRepository

ACCOUNT = "acct-1"
NOW = datetime(2026, 10, 15, 13, 0, 0, tzinfo=UTC)


class FakeExchange:
    def __init__(self, orders):
        self._orders = orders
        self.calls: list[dict] = []

    def list_orders(self, symbol=None, limit=100):
        self.calls.append({"symbol": symbol, "limit": limit})
        return list(self._orders)[:limit]


def _exchange_order(order_id: str, symbol: str, *, minutes_ago: int) -> ExchangeOrder:
    created = NOW - timedelta(minutes=minutes_ago)
    return ExchangeOrder(
        order_id=order_id,
        client_order_id=f"cid-{order_id}",
        symbol=symbol,
        side="BUY",
        position_side="LONG",
        status="FILLED",
        order_type="MARKET",
        quantity=Decimal(100),
        executed_quantity=Decimal(100),
        price=Decimal("0.08"),
        average_price=Decimal("0.08"),
        time_in_force="IOC",
        created_at=created,
        updated_at=created,
        reduce_only=False,
    )


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'recon.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _seed_fill(db: Session, *, fill_id: str, order_id: str) -> None:
    repo = StreamRepository(db, user_id="u-1", trading_account_id=ACCOUNT)
    repo.record(
        FillEvent(
            exchange_fill_id=fill_id,
            exchange_order_id=order_id,
            symbol="",  # 推送里没有 symbol
            position_side="LONG",
            order_side="BUY",
            quantity=Decimal(100),
            value=Decimal("7.99"),
            fee=Decimal("0.006392"),
            realize_pnl=Decimal("0.01"),
            direction="TAKER",
            filled_at=NOW,
            account_version=1,
            raw={},
        )
    )
    db.commit()


def _seed_order(db: Session, order_id: str, *, minutes_ago: int = 1) -> None:
    repo = StreamRepository(db, user_id="u-1", trading_account_id=ACCOUNT)
    repo.record(
        OrderEvent(
            exchange_order_id=order_id,
            client_order_id=f"cid-{order_id}",
            symbol="",  # 推送里也没有 symbol
            position_side="LONG",
            order_side="BUY",
            order_type="MARKET",
            status="FILLED",
            order_source="WEB",
            reduce_only=False,
            position_tpsl=False,
            trigger_price=Decimal(0),
            quantity=Decimal(100),
            cum_fill_quantity=Decimal(100),
            cum_fill_value=Decimal("7.99"),
            cum_fill_fee=Decimal("0.006392"),
            cum_realize_pnl=Decimal("0.01"),
            cum_liquidate_fee=Decimal(0),
            taker_fee_rate=Decimal("0.0008"),
            maker_fee_rate=Decimal("0.0002"),
            created_at=NOW - timedelta(minutes=minutes_ago),
            updated_at=NOW - timedelta(minutes=minutes_ago),
            account_version=1,
            raw={},
        )
    )
    db.commit()


def _reconciler(db: Session, orders) -> tuple[OrderHistoryReconciler, FakeExchange]:
    exchange = FakeExchange(orders)
    return OrderHistoryReconciler(db, trading_account_id=ACCOUNT, exchange=exchange), exchange


def test_backfills_fill_symbol_from_rest(db):
    """推送没有 symbol，只有 REST 有 —— 这是它唯一的补法。"""

    _seed_fill(db, fill_id="f-1", order_id="o-1")
    assert db.scalar(select(ExchangeFillRecord)).symbol is None

    reconciler, exchange = _reconciler(db, [_exchange_order("o-1", "DOGESUSDT", minutes_ago=5)])
    outcome = reconciler.run_once()
    db.commit()

    assert outcome.symbols_backfilled == 1
    assert db.scalar(select(ExchangeFillRecord)).symbol == "DOGESUSDT"
    assert exchange.calls == [{"symbol": None, "limit": 100}]


def test_backfills_all_orders_of_one_symbol(db):
    _seed_fill(db, fill_id="f-1", order_id="o-1")
    _seed_fill(db, fill_id="f-2", order_id="o-2")

    reconciler, _ = _reconciler(
        db,
        [
            _exchange_order("o-1", "DOGESUSDT", minutes_ago=5),
            _exchange_order("o-2", "DOGESUSDT", minutes_ago=4),
        ],
    )
    outcome = reconciler.run_once()
    db.commit()

    assert outcome.symbols_backfilled == 2
    assert {row.symbol for row in db.scalars(select(ExchangeFillRecord))} == {"DOGESUSDT"}


def test_backfills_order_event_symbol_too(db):
    """订单表的 symbol 也得补。

    两张表「未知」的表示不同：成交表是 NULL，订单表是空串。只补 NULL 的话
    订单表的 symbol 会永远为空 —— 一张恒为空的列是个陷阱。
    """

    _seed_order(db, "o-1", minutes_ago=5)
    assert db.scalar(select(ExchangeOrderEvent)).symbol == ""

    reconciler, _ = _reconciler(db, [_exchange_order("o-1", "DOGESUSDT", minutes_ago=5)])
    outcome = reconciler.run_once()
    db.commit()

    assert outcome.symbols_backfilled == 1
    assert db.scalar(select(ExchangeOrderEvent)).symbol == "DOGESUSDT"


def test_does_not_touch_fills_that_already_have_a_symbol(db):
    _seed_fill(db, fill_id="f-1", order_id="o-1")
    row = db.scalar(select(ExchangeFillRecord))
    row.symbol = "XRPUSDT"
    db.commit()

    reconciler, _ = _reconciler(db, [_exchange_order("o-1", "DOGESUSDT", minutes_ago=5)])
    outcome = reconciler.run_once()

    assert outcome.symbols_backfilled == 0
    assert db.scalar(select(ExchangeFillRecord)).symbol == "XRPUSDT"


def test_reports_orders_missed_by_the_stream(db):
    """比「我们最早见过的订单」新、却没见过的，就是 WS 漏掉的。"""

    _seed_order(db, "o-1", minutes_ago=10)

    reconciler, _ = _reconciler(
        db,
        [
            _exchange_order("o-9", "DOGESUSDT", minutes_ago=5),  # 没见过 → 漏单
            _exchange_order("o-1", "DOGESUSDT", minutes_ago=10),  # 见过
        ],
    )
    outcome = reconciler.run_once()

    assert outcome.missed_order_ids == ("o-9",)
    assert outcome.has_gap is True


def test_ignores_orders_older_than_our_earliest_record(db):
    """监听开始之前的订单本来就没人看着，不算漏单。"""

    _seed_order(db, "o-5", minutes_ago=10)

    reconciler, _ = _reconciler(
        db,
        [
            _exchange_order("o-1", "DOGESUSDT", minutes_ago=600),  # 远早于我们开始
            _exchange_order("o-5", "DOGESUSDT", minutes_ago=10),
        ],
    )
    outcome = reconciler.run_once()

    assert outcome.missed_order_ids == ()


def test_reports_no_gap_when_there_is_no_baseline(db):
    """一条订单事件都没有时没有基准可言 —— 不能把全部历史都报成漏单。"""

    reconciler, _ = _reconciler(db, [_exchange_order("o-1", "DOGESUSDT", minutes_ago=600)])
    outcome = reconciler.run_once()

    assert outcome.missed_order_ids == ()


def test_records_the_gap_in_the_merged_error_table(db):
    """漏单要留痕，不能只打日志 —— 否则「这段记录不完整」这件事事后无从查证。"""

    _seed_order(db, "o-1", minutes_ago=10)
    reconciler, _ = _reconciler(db, [_exchange_order("o-9", "DOGESUSDT", minutes_ago=5)])
    reconciler.run_once()
    db.commit()

    row = db.scalar(select(CollectorError).where(CollectorError.collector == GAP_COLLECTOR))
    assert row is not None
    assert row.message == "observation_gap"
    assert row.occurrences == 1


def test_gap_counter_merges_across_runs(db):
    """归并计数：反复看到同一个问题只累加 occurrences，不撑爆表。"""

    _seed_order(db, "o-1", minutes_ago=10)
    orders = [_exchange_order("o-9", "DOGESUSDT", minutes_ago=5)]

    reconciler, _ = _reconciler(db, orders)
    reconciler.run_once()
    db.commit()
    second, _ = _reconciler(db, orders)
    second.run_once()
    db.commit()

    rows = db.scalars(
        select(CollectorError).where(CollectorError.collector == GAP_COLLECTOR)
    ).all()
    assert len(rows) == 1
    assert rows[0].occurrences == 2


def _run(db: Session, orders, reported: set[str]) -> object:
    return OrderHistoryReconciler(
        db, trading_account_id=ACCOUNT, exchange=FakeExchange(orders), reported=reported
    ).run_once()


def test_the_same_gap_is_only_reported_once(db):
    """漏掉的订单**永远不会**出现在我们这边，所以「REST 有、我们没有」这个判据
    每轮都会为同一笔订单成立。

    不带已报集合的话，一个历史缺口会被每 5 分钟重报一次 ERROR，直到它滑出
    REST 的 100 条窗口 —— 而 ``occurrences`` 也会把「同一个旧洞数了很多遍」
    记成很多次漏采。那会把 ERROR 训练成噪音。
    """

    _seed_order(db, "o-1", minutes_ago=10)
    orders = [_exchange_order("o-9", "DOGESUSDT", minutes_ago=5)]
    reported: set[str] = set()

    first = _run(db, orders, reported)
    second = _run(db, orders, reported)

    assert first.missed_order_ids == ("o-9",)
    assert first.has_gap is True
    assert second.missed_order_ids == ()
    assert second.has_gap is False


def test_a_genuinely_new_gap_is_still_reported(db):
    _seed_order(db, "o-1", minutes_ago=10)
    reported: set[str] = set()

    first = _run(db, [_exchange_order("o-9", "DOGESUSDT", minutes_ago=5)], reported)
    second = _run(
        db,
        [
            _exchange_order("o-9", "DOGESUSDT", minutes_ago=5),
            _exchange_order("o-8", "DOGESUSDT", minutes_ago=4),
        ],
        reported,
    )

    assert first.missed_order_ids == ("o-9",)
    assert second.missed_order_ids == ("o-8",)


def test_reported_set_is_pruned_to_the_current_window(db):
    """滑出 REST 窗口的订单不会再出现，没必要一直记着 —— 否则这个集合会随
    进程寿命无上限地涨。"""

    _seed_order(db, "o-1", minutes_ago=10)
    reported: set[str] = set()

    _run(db, [_exchange_order("o-9", "DOGESUSDT", minutes_ago=5)], reported)
    assert reported == {"o-9"}

    # 下一轮的窗口里已经没有 o-9
    _run(db, [_exchange_order("o-1", "DOGESUSDT", minutes_ago=10)], reported)
    assert reported == set()


def test_gap_counter_counts_new_gaps_not_repeat_sightings(db):
    """``occurrences`` 要代表「新发现了几次漏采」，不是「同一个旧洞数了几遍」。"""

    _seed_order(db, "o-1", minutes_ago=10)
    orders = [_exchange_order("o-9", "DOGESUSDT", minutes_ago=5)]
    reported: set[str] = set()

    for _ in range(3):
        _run(db, orders, reported)
        db.commit()

    row = db.scalar(select(CollectorError).where(CollectorError.collector == GAP_COLLECTOR))
    assert row.occurrences == 1


def test_returns_inspected_count(db):
    reconciler, _ = _reconciler(
        db,
        [
            _exchange_order("o-1", "DOGESUSDT", minutes_ago=5),
            _exchange_order("o-2", "XRPUSDT", minutes_ago=4),
        ],
    )

    assert reconciler.run_once().inspected == 2

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
        "quantity": Decimal(100),
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
        "quantity": Decimal(100),
        "cum_fill_quantity": Decimal(100),
        "cum_fill_value": Decimal("7.99"),
        "cum_fill_fee": Decimal("0.006392"),
        "cum_realize_pnl": Decimal("0.01"),
        "cum_liquidate_fee": Decimal(0),
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
        "cum_deposit": Decimal(0),
        "cum_withdraw": Decimal(0),
        "cum_transfer_in": Decimal(40000),
        "cum_transfer_out": Decimal(0),
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
    repo.record(_order(status="PENDING", cum_realize_pnl=Decimal(0)))
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
    assert (
        db.scalar(
            select(ExchangeFillRecord).where(ExchangeFillRecord.exchange_fill_id == "f-2")
        ).symbol
        is None
    )

    repo.record(_order(exchange_order_id="o-2"))
    db.commit()
    assert (
        db.scalar(
            select(ExchangeFillRecord).where(ExchangeFillRecord.exchange_fill_id == "f-2")
        ).symbol
        == "DOGESUSDT"
    )


def test_account_sample_is_idempotent_on_version(db):
    repo = _repo(db)
    assert repo.record(_sample()) is True
    assert repo.record(_sample()) is False
    db.commit()

    assert len(db.scalars(select(ExchangeAccountSample)).all()) == 1


def test_record_returns_false_for_unknown_event_types(db):
    repo = _repo(db)
    assert repo.record(object()) is False  # type: ignore[arg-type]

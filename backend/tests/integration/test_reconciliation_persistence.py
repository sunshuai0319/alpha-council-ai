from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import (
    AccountSnapshot,
    Base,
    Fill,
    Order,
    PnlSnapshot,
    Position,
    TradingAccount,
    User,
)
from app.exchange.base import ExchangeBalance, ExchangeFill, ExchangeOrder, ExchangePosition
from app.reconciliation.service import ReconciliationService


class ExchangeFixture:
    def get_order(self, order_id: str) -> ExchangeOrder:
        return ExchangeOrder(
            order_id=order_id,
            client_order_id="client-1",
            symbol="BTC-USDT",
            side="BUY",
            position_side="LONG",
            status="FILLED",
            order_type="MARKET",
            quantity=Decimal("0.01"),
            executed_quantity=Decimal("0.01"),
            price=Decimal(100),
            average_price=Decimal(100),
            time_in_force=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def get_trades(self, symbol=None, order_id=None, limit=100) -> list[ExchangeFill]:
        return [
            ExchangeFill(
                fill_id="fill-1",
                order_id="exchange-1",
                symbol="BTC-USDT",
                side="BUY",
                position_side="LONG",
                quantity=Decimal("0.01"),
                price=Decimal(100),
                fee=Decimal("0.01"),
                realized_pnl=Decimal(2),
                filled_at=datetime.now(UTC),
            )
        ]

    def get_positions(self) -> list[ExchangePosition]:
        return [
            ExchangePosition(
                position_id="position-1",
                symbol="BTC-USDT",
                side="LONG",
                quantity=Decimal("0.01"),
                entry_value=Decimal(100),
                margin=Decimal(10),
                leverage=10,
                unrealized_pnl=Decimal(3),
                liquidation_price=None,
            )
        ]

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", Decimal(1003), Decimal(1000), Decimal(3), Decimal(3))]


class NoTradeFeedFixture(ExchangeFixture):
    """虚拟盘：没有成交流水接口，get_trades 一旦被调用就说明还在撒谎。"""

    supports_trade_fills = False

    def __init__(self, balance: str, unrealized: str = "0") -> None:
        self._balance = Decimal(balance)
        self._unrealized = Decimal(unrealized)

    def get_trades(self, symbol=None, order_id=None, limit=100):
        raise AssertionError("virtual account has no trade feed; it must not be queried")

    def get_positions(self) -> list[ExchangePosition]:
        position = super().get_positions()[0]
        return [ExchangePosition(**{**position.__dict__, "unrealized_pnl": self._unrealized})]

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", self._balance, self._balance, Decimal(0), self._unrealized)]


def _scoped_db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reconcile.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="user-reconcile", clerk_user_id="clerk-reconcile"))
    db.add(TradingAccount(id="account-reconcile", user_id="user-reconcile"))
    db.commit()
    return db


def _reconcile(service: ReconciliationService, exchange: ExchangeFixture):
    return service.reconcile(
        exchange,
        user_id="user-reconcile",
        trading_account_id="account-reconcile",
        order_ids=["exchange-1"],
        symbol="BTC-USDT",
    )


def test_reconciliation_skips_trade_feed_when_exchange_has_none(tmp_path) -> None:
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    result = _reconcile(service, NoTradeFeedFixture("1000"))

    assert result.fills_available is False
    # None 表示「不可得」，而不是「等于 0」——0 会让亏损熔断永远不触发。
    assert result.realized_pnl is None
    assert db.scalars(select(Fill)).all() == []
    assert len(db.scalars(select(AccountSnapshot)).all()) == 1


def test_reconciliation_matches_live_virtual_account_round_trip(tmp_path) -> None:
    """用真实虚拟盘一笔开+平（BTCSUSDT 0.0001）的余额复现公式。

    实测：开仓前 19999.95976631；开仓后 19999.95354940（该仓位 ``openFee`` 实测
    0.00621691，余额净减与之**逐位相等**，未实现盈亏 0.00011 没有进余额 —— 即
    余额只随手续费与已实现盈亏变动）；平仓后 19999.94732250。
    """

    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    _reconcile(service, NoTradeFeedFixture("19999.95976631", "0"))
    _reconcile(service, NoTradeFeedFixture("19999.95354940", "0.00011"))
    _reconcile(service, NoTradeFeedFixture("19999.94732250", "0"))

    snapshots = db.scalars(select(PnlSnapshot).order_by(PnlSnapshot.id)).all()
    assert snapshots[0].realized_pnl == Decimal(0)  # 首次观测，无基准
    # 等于该仓位真实的手续费；未实现盈亏 0.00011 不应污染已实现盈亏
    assert snapshots[1].realized_pnl == pytest.approx(Decimal("-0.00621691"), abs=Decimal("1e-9"))
    assert snapshots[2].realized_pnl == pytest.approx(Decimal("-0.01244381"), abs=Decimal("1e-9"))


class _FlatFixture(NoTradeFeedFixture):
    def get_positions(self) -> list[ExchangePosition]:
        return []


def test_reconciliation_positions_reflect_current_state_not_history(tmp_path) -> None:
    """平掉的仓位必须从结果里消失：positions 是当前状态，不是事件流。"""
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    assert len(_reconcile(service, NoTradeFeedFixture("1000")).positions) == 1
    assert _reconcile(service, _FlatFixture("1000")).positions == []


def test_reconciliation_derives_realized_pnl_from_balance_without_trade_feed(tmp_path) -> None:
    """无成交流水时，已实现盈亏 = 余额变化的累加；未实现盈亏单独列示、不参与推导。"""
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    _reconcile(service, NoTradeFeedFixture("1000", "0"))
    _reconcile(service, NoTradeFeedFixture("1010", "4"))

    snapshots = db.scalars(select(PnlSnapshot).order_by(PnlSnapshot.id)).all()
    # Δbalance = 10；未实现盈亏 4 只写进 unrealized_pnl，不污染已实现盈亏
    assert [snapshot.realized_pnl for snapshot in snapshots] == [Decimal(0), Decimal(10)]
    assert snapshots[1].unrealized_pnl == Decimal(4)


def test_reconciliation_persists_exchange_state_and_is_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reconcile.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="user-reconcile", clerk_user_id="clerk-reconcile"))
        db.add(TradingAccount(id="account-reconcile", user_id="user-reconcile"))
        db.commit()
        service = ReconciliationService(db=db)
        result = service.reconcile(
            ExchangeFixture(),
            user_id="user-reconcile",
            trading_account_id="account-reconcile",
            order_ids=["exchange-1"],
            symbol="BTC-USDT",
        )
        service.reconcile(
            ExchangeFixture(),
            user_id="user-reconcile",
            trading_account_id="account-reconcile",
            order_ids=["exchange-1"],
            symbol="BTC-USDT",
        )
        assert result.realized_pnl == Decimal(2)
        assert len(db.scalars(select(Order)).all()) == 1
        assert len(db.scalars(select(Fill)).all()) == 1
        assert len(db.scalars(select(Position)).all()) == 1
        assert len(db.scalars(select(AccountSnapshot)).all()) == 2
        assert len(db.scalars(select(PnlSnapshot)).all()) == 2


def test_reconciliation_closes_local_rows_when_the_exchange_position_is_gone(tmp_path) -> None:
    """本地仓位的 status 必须跟着交易所走，否则会留下幽灵持仓。

    实测触发：在交易所手工开了一笔 BTC 空头，worker 恰好在那个对账窗口里把它写成
    OPEN；随后手工平掉，交易所返回空仓位，本地那行却一直停在 OPEN —— 账本页面把它
    当活仓位显示，持仓管理也会对着不存在的仓位下单。
    """
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    _reconcile(service, NoTradeFeedFixture("1000"))
    assert [row.status for row in db.scalars(select(Position)).all()] == ["OPEN"]

    _reconcile(service, _FlatFixture("1000"))

    rows = db.scalars(select(Position)).all()
    assert rows != [], "行要保留供审计，只改状态"
    assert [row.status for row in rows] == ["CLOSED"]
    assert rows[0].quantity == Decimal(0), "已平仓位不该继续报告数量"


def test_reopening_the_same_symbol_refreshes_the_entry_price(tmp_path) -> None:
    """复用行时必须刷新 entry_price。

    实测踩到：平仓后重开同一 symbol 会复用同一个 Position 行，而 entry_price 只在
    建行时写一次，于是本地记着旧仓位的开仓价（77309.5）而实际是 77316.3 ——
    盈亏与后续的持仓管理都会基于错误成本。
    """
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    _reconcile(service, NoTradeFeedFixture("1000"))
    first = db.scalars(select(Position)).all()[0].entry_price

    _reconcile(service, _FlatFixture("1000"))
    # 用不同的入场价重开
    moved = ExchangeFixture()
    moved.get_positions = lambda: [  # type: ignore[method-assign]
        ExchangePosition(
            position_id="position-2",
            symbol="BTC-USDT",
            side="LONG",
            quantity=Decimal("0.02"),
            entry_value=Decimal(3),  # 3 / 0.02 = 150，与首个仓位的 100 不同
            margin=Decimal(30),
            leverage=10,
            unrealized_pnl=Decimal(0),
            liquidation_price=None,
        )
    ]
    _reconcile(service, moved)

    rows = db.scalars(select(Position)).all()
    assert len(rows) == 1
    assert rows[0].entry_price == Decimal(150), f"旧值 {first} 未被刷新"
    assert rows[0].quantity == Decimal("0.02")


def test_reopening_the_same_symbol_marks_the_row_open_again(tmp_path) -> None:
    """唯一键是 (user, account, symbol)，平仓后重开会复用同一行，状态要能回头。"""
    db = _scoped_db(tmp_path)
    service = ReconciliationService(db=db)

    _reconcile(service, NoTradeFeedFixture("1000"))
    _reconcile(service, _FlatFixture("1000"))
    _reconcile(service, NoTradeFeedFixture("1000"))

    rows = db.scalars(select(Position)).all()
    assert len(rows) == 1, "同一 symbol 不该产生第二行"
    assert rows[0].status == "OPEN"
    assert rows[0].quantity == Decimal("0.01")

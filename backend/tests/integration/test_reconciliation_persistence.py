from datetime import UTC, datetime
from decimal import Decimal

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

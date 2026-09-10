from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AccountSnapshot, Fill, Order, PnlSnapshot, Position
from app.exchange.base import ExchangeClient, ExchangeFill, ExchangeOrder, ExchangePosition


@dataclass
class ReconciliationStore:
    orders: dict[str, ExchangeOrder] = field(default_factory=dict)
    fills: dict[str, ExchangeFill] = field(default_factory=dict)
    positions: dict[str, ExchangePosition] = field(default_factory=dict)

    @property
    def realized_pnl(self) -> Decimal:
        return sum((fill.realized_pnl for fill in self.fills.values()), Decimal(0))


@dataclass(frozen=True)
class ReconciliationResult:
    orders: list[ExchangeOrder]
    fills: list[ExchangeFill]
    positions: list[ExchangePosition]
    realized_pnl: Decimal


class ReconciliationService:
    def __init__(self, store: ReconciliationStore | None = None, db: Session | None = None) -> None:
        self.store = store or ReconciliationStore()
        self.db = db

    def reconcile(
        self,
        exchange: ExchangeClient,
        *,
        user_id: str | None = None,
        trading_account_id: str | None = None,
        order_ids: list[str] | None = None,
        symbol: str | None = None,
    ) -> ReconciliationResult:
        synced_orders: list[ExchangeOrder] = []
        for order_id in order_ids or []:
            order = exchange.get_order(order_id)
            self.store.orders[order.order_id] = order
            synced_orders.append(order)
        fills = exchange.get_trades(symbol=symbol)
        for fill in fills:
            self.store.fills[fill.fill_id] = fill
        positions = exchange.get_positions()
        for position in positions:
            self.store.positions[f"{position.symbol}:{position.side}"] = position
        if self.db is not None and user_id and trading_account_id:
            self._persist_db(
                exchange,
                user_id=user_id,
                trading_account_id=trading_account_id,
                orders=synced_orders,
                fills=fills,
                positions=positions,
            )
        return ReconciliationResult(
            orders=synced_orders,
            fills=list(self.store.fills.values()),
            positions=list(self.store.positions.values()),
            realized_pnl=self.store.realized_pnl,
        )

    def _persist_db(
        self,
        exchange: ExchangeClient,
        *,
        user_id: str,
        trading_account_id: str,
        orders: list[ExchangeOrder],
        fills: list[ExchangeFill],
        positions: list[ExchangePosition],
    ) -> None:
        if self.db is None:
            return
        db = self.db
        order_rows: dict[str, Order] = {}
        for item_order in orders:
            row = db.scalar(
                select(Order).where(Order.user_id == user_id, Order.exchange_order_id == item_order.order_id)
            )
            if row is None:
                row = Order(
                    id=str(uuid4()),
                    user_id=user_id,
                    trading_account_id=trading_account_id,
                    symbol=item_order.symbol,
                    side=item_order.side,
                    order_type=item_order.order_type,
                    quantity=item_order.quantity,
                    client_order_id=item_order.client_order_id,
                    exchange_order_id=item_order.order_id,
                )
                db.add(row)
            row.status = item_order.status
            row.response_payload = item_order.raw
            order_rows[item_order.order_id] = row
        db.flush()
        for item_fill in fills:
            if db.scalar(
                select(Fill).where(Fill.user_id == user_id, Fill.exchange_fill_id == item_fill.fill_id)
            ) is not None:
                continue
            order = order_rows.get(item_fill.order_id) or db.scalar(
                select(Order).where(Order.user_id == user_id, Order.exchange_order_id == item_fill.order_id)
            )
            if order is None:
                continue
            db.add(
                Fill(
                    id=str(uuid4()),
                    user_id=user_id,
                    order_id=order.id,
                    exchange_fill_id=item_fill.fill_id,
                    quantity=item_fill.quantity,
                    price=item_fill.price,
                    fee=item_fill.fee,
                    filled_at=item_fill.filled_at,
                )
            )
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
        balance = next(iter(exchange.get_balances()), None)
        now = datetime.now(UTC)
        if balance is not None:
            db.add(
                AccountSnapshot(
                    user_id=user_id,
                    trading_account_id=trading_account_id,
                    captured_at=now,
                    balance=balance.balance,
                    available_margin=balance.available_balance,
                    equity=balance.balance + balance.unrealized_pnl,
                    margin_ratio=None,
                )
            )
            db.add(
                PnlSnapshot(
                    user_id=user_id,
                    trading_account_id=trading_account_id,
                    captured_at=now,
                    equity=balance.balance + balance.unrealized_pnl,
                    realized_pnl=sum((fill.realized_pnl for fill in fills), Decimal(0)),
                    unrealized_pnl=sum((position.unrealized_pnl for position in positions), Decimal(0)),
                    drawdown_pct=0,
                )
            )
        db.commit()

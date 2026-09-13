from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AccountSnapshot, Fill, MarketSnapshot, Order, PnlSnapshot, Position
from app.exchange.base import (
    ExchangeBalance,
    ExchangeClient,
    ExchangeFill,
    ExchangeOrder,
    ExchangePosition,
)


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
    #: ``None`` 表示账户没有成交流水，本次无法观测到已实现盈亏 —— 与「等于 0」
    #: 是两回事：假的 0 会让日亏损与连亏熔断永远不触发。
    realized_pnl: Decimal | None
    fills_available: bool = True


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
        fills_available = bool(getattr(exchange, "supports_trade_fills", True))
        fills = exchange.get_trades(symbol=symbol) if fills_available else []
        for fill in fills:
            self.store.fills[fill.fill_id] = fill
        positions = exchange.get_positions()
        # positions 是当前状态而不是事件流：整体替换，否则已平掉的仓位会永远留在
        # 结果里（实测平仓后 result.positions 仍报 1，而交易所返回空）。
        self.store.positions = {f"{position.symbol}:{position.side}": position for position in positions}
        if self.db is not None and user_id and trading_account_id:
            self._persist_db(
                exchange,
                user_id=user_id,
                trading_account_id=trading_account_id,
                orders=synced_orders,
                fills=fills,
                positions=positions,
                fills_available=fills_available,
            )
        return ReconciliationResult(
            orders=synced_orders,
            fills=list(self.store.fills.values()),
            positions=list(self.store.positions.values()),
            realized_pnl=self.store.realized_pnl if fills_available else None,
            fills_available=fills_available,
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
        fills_available: bool = True,
    ) -> None:
        if self.db is None:
            return
        db = self.db
        balance = next(iter(exchange.get_balances()), None)
        # 必须在写入本次快照之前算，否则「上一笔」就是本次自己，差值恒为 0。
        realized_pnl = self._realized_pnl(
            db,
            user_id=user_id,
            trading_account_id=trading_account_id,
            balance=balance,
            fills_available=fills_available,
        )
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
            entry_price = (
                item_position.entry_value / item_position.quantity
                if item_position.quantity
                else Decimal(0)
            )
            position_row.side = item_position.side
            position_row.quantity = item_position.quantity
            # entry_price 必须每次刷新，不能只在建行时写：平仓后重开会复用同一行
            # （唯一键是 user+account+symbol），不刷新就会一直记着上一个仓位的成本。
            position_row.entry_price = entry_price
            # mark_price 语义是「当前标记价格」，不能拿开仓均价顶替：WEEX position
            # 接口不返回价格字段（见 docs/weex-virtual-api.md），唯一价格源是行情快照
            # 的 markPrice / lastPrice。每轮对账用最近一次行情刷新，前端持仓表的
            # Mark 列才会随行情走，而不是永远停在开仓价。
            mark_price = self._latest_mark_price(db, item_position.symbol)
            if mark_price is not None:
                position_row.mark_price = mark_price
            position_row.leverage = item_position.leverage
            position_row.unrealized_pnl = item_position.unrealized_pnl
            # 同一 symbol 平仓后重开会复用这行（唯一键是 user+account+symbol），
            # 所以每次观测到仓位都要把状态翻回 OPEN。
            position_row.status = "OPEN"

        # 交易所没回报的仓位必须在本地跟着平掉。positions 是当前状态而不是事件流 ——
        # 不做这步，任何不走本系统 CLOSE 路径的退出（止损触发、人工平仓、强平）都会
        # 在本地留下永远 OPEN 的幽灵仓位，账本页面把它当活仓位显示。
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
            stale_row.effective_stop = None
            stale_row.near_target_at = None
        now = datetime.now(UTC)
        if balance is not None:
            unrealized = sum((position.unrealized_pnl for position in positions), Decimal(0))
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
                    realized_pnl=realized_pnl,
                    unrealized_pnl=unrealized,
                    drawdown_pct=0,
                )
            )
        db.commit()

    def _latest_mark_price(self, db: Session, symbol: str) -> Decimal | None:
        """该 symbol 最近一次行情快照的标记价格。

        market_snapshots 是全局行情表（与用户无关）；最近一条就是当前观测价。
        缺 markPrice 时回退 lastPrice；完全没有行情时返回 None（不编造现价）。
        """

        row = db.scalar(
            select(MarketSnapshot)
            .where(MarketSnapshot.symbol == symbol)
            .order_by(MarketSnapshot.captured_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        return row.mark_price if row.mark_price is not None else row.last_price

    def _realized_pnl(
        self,
        db: Session,
        *,
        user_id: str,
        trading_account_id: str,
        balance: ExchangeBalance | None,
        fills_available: bool,
    ) -> Decimal:
        """累计已实现盈亏。

        有成交流水时用 fill 求和；没有时（虚拟盘）改为余额差分：钱包余额只随
        手续费与已实现盈亏变动，未实现盈亏是单独列示的（实测开仓后余额净减与仓位
        ``openFee`` 逐位相等），所以两次对账之间的余额变化就是区间已实现盈亏。
        """

        if fills_available:
            return sum((fill.realized_pnl for fill in self.store.fills.values()), Decimal(0))
        if balance is None:
            return Decimal(0)
        previous_account = db.scalar(
            select(AccountSnapshot)
            .where(
                AccountSnapshot.user_id == user_id,
                AccountSnapshot.trading_account_id == trading_account_id,
            )
            .order_by(AccountSnapshot.captured_at.desc(), AccountSnapshot.id.desc())
            .limit(1)
        )
        previous_pnl = db.scalar(
            select(PnlSnapshot)
            .where(
                PnlSnapshot.user_id == user_id,
                PnlSnapshot.trading_account_id == trading_account_id,
            )
            .order_by(PnlSnapshot.captured_at.desc(), PnlSnapshot.id.desc())
            .limit(1)
        )
        if previous_account is None or previous_pnl is None:
            return Decimal(0)  # 首次观测，没有可比较的基准
        return previous_pnl.realized_pnl + (balance.balance - previous_account.balance)

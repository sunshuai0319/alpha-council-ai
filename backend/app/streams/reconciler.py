"""REST ``order/history`` 兜底对账。

WS 有两个已知缺口，都只能靠 REST 补：

1. **推送里没有 symbol。** 实测：``fill`` 与 ``orders`` 两个频道的行都只有
   ``coin`` 与 ``orderId``，没有 ``symbol``（WEEX 文档列了这个字段，虚拟盘不推）。
   REST ``order/history`` 带 —— 用它按 orderId 回填。
2. **WS 断线期间的成交不会重放。** REST 能看到最近 100 条终态订单，
   凡是我们没通过 WS 见过的，就是漏掉的。

**能补什么、不能补什么**（别把兜底当万能）：

- 能补：``symbol``
- **不能补**：漏掉的成交带来的盈亏 —— REST 没有 ``realizePnl``，漏一笔就是
  永久缺一笔。所以漏单只能**标记**，让下游知道这段记录不完整。

这个模块只做「给一个 Session，跑一轮对账」。会话与线程的管理在
``app/streams/listener.py`` —— 保持纯逻辑才好单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.collector_errors import record_merged_error
from app.db.models import ExchangeFillRecord, ExchangeOrderEvent
from app.exchange.base import ExchangeClient, ExchangeOrder

logger = logging.getLogger(__name__)

#: 漏单计数在 collector_errors 里的来源标识。
GAP_COLLECTOR = "weex_private_streams"

#: 归并用的稳定消息。**不要**把 orderId 拼进来 —— 那会让每次漏单都产生新行，
#: 归并计数就失效了。具体漏了哪些订单打到日志里。
GAP_MESSAGE = "observation_gap"

#: REST 窗口上限。虚拟盘没有分页（实测 page 被忽略），这就是能看到的全部。
REST_WINDOW = 100


@dataclass
class ReconcileOutcome:
    inspected: int = 0
    symbols_backfilled: int = 0
    missed_order_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_gap(self) -> bool:
        return bool(self.missed_order_ids)


class OrderHistoryReconciler:
    def __init__(
        self,
        db: Session,
        *,
        trading_account_id: str,
        exchange: ExchangeClient,
    ) -> None:
        self.db = db
        self.trading_account_id = trading_account_id
        self.exchange = exchange

    def run_once(self) -> ReconcileOutcome:
        orders = self.exchange.list_orders(limit=REST_WINDOW)
        outcome = ReconcileOutcome(inspected=len(orders), missed_order_ids=self._missed(orders))
        outcome.symbols_backfilled = self._backfill_symbols(orders)
        if outcome.has_gap:
            # 具体漏了哪些放日志；库里只累加计数。
            logger.error(
                "private stream missed %d order(s), their PnL is unrecoverable: %s",
                len(outcome.missed_order_ids),
                list(outcome.missed_order_ids[:10]),
            )
            record_merged_error(self.db, collector=GAP_COLLECTOR, message=GAP_MESSAGE)
        return outcome

    def _backfill_symbols(self, orders: list[ExchangeOrder]) -> int:
        """把 REST 里的 symbol 补到「还不知道品种」的行，返回补上的行数。

        两张表都要补。注意它们的「未知」表示不同 —— 成交表用 ``NULL``
        （``symbol`` 可空），订单表用空串（``symbol`` 非空、默认 ``""``）。
        一张恒为空的列是陷阱：有人 join 它只会静默拿到空结果。

        只补未知的 —— 已有值的不动，免得把更可信的来源覆盖掉。
        """

        symbols = {order.order_id: order.symbol for order in orders if order.symbol}
        if not symbols:
            return 0
        order_ids = list(symbols)
        updated = 0

        fill_rows = self.db.scalars(
            select(ExchangeFillRecord).where(
                ExchangeFillRecord.trading_account_id == self.trading_account_id,
                ExchangeFillRecord.symbol.is_(None),
                ExchangeFillRecord.exchange_order_id.in_(order_ids),
            )
        ).all()
        for row in fill_rows:
            row.symbol = symbols[row.exchange_order_id]
        updated += len(fill_rows)

        event_rows = self.db.scalars(
            select(ExchangeOrderEvent).where(
                ExchangeOrderEvent.trading_account_id == self.trading_account_id,
                ExchangeOrderEvent.symbol == "",
                ExchangeOrderEvent.exchange_order_id.in_(order_ids),
            )
        ).all()
        for event in event_rows:
            event.symbol = symbols[event.exchange_order_id]
        updated += len(event_rows)

        return updated

    def _missed(self, orders: list[ExchangeOrder]) -> tuple[str, ...]:
        """REST 里有、我们却没见过的订单。

        基准取**我们最早见过的那条订单事件**：比它更早的订单发生在监听上线之前，
        本来就没人看着，不能算漏。一条订单事件都没有时没有基准可言 —— 此时把
        整个历史都报成漏单毫无意义，一律返回空。
        """

        earliest = self.db.scalar(
            select(func.min(ExchangeOrderEvent.exchange_created_at)).where(
                ExchangeOrderEvent.trading_account_id == self.trading_account_id
            )
        )
        if earliest is None:
            return ()
        # SQLite 取回来是 naive 的（Postgres 是 aware）—— 统一按 UTC 处理，否则与
        # REST 的 aware 时间比较会炸。见 app/positions/manager.py 同样的处理。
        if earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=UTC)

        known = set(
            self.db.scalars(
                select(ExchangeOrderEvent.exchange_order_id).where(
                    ExchangeOrderEvent.trading_account_id == self.trading_account_id
                )
            ).all()
        )
        return tuple(
            order.order_id
            for order in orders
            if order.order_id not in known
            and order.created_at is not None
            and order.created_at >= earliest
        )

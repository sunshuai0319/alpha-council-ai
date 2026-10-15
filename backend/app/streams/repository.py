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

from dataclasses import dataclass, field
from decimal import Decimal

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
    def __init__(self, store: ReconciliationStore | None = None) -> None:
        self.store = store or ReconciliationStore()

    def reconcile(
        self,
        exchange: ExchangeClient,
        *,
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
        return ReconciliationResult(
            orders=synced_orders,
            fills=list(self.store.fills.values()),
            positions=list(self.store.positions.values()),
            realized_pnl=self.store.realized_pnl,
        )

import hashlib
from decimal import Decimal
from typing import Any

import httpx

from app.domain.enums import Action, RiskStatus
from app.domain.schemas import ExecutionResult, RiskDecision, TradeProposal
from app.exchange.base import ExchangeClient, ExchangeError, ExchangeOrder, OrderRequest


def stable_client_order_id(proposal_id: str) -> str:
    return f"alpha-{hashlib.sha256(proposal_id.encode()).hexdigest()[:28]}"


def _order_request_for(
    proposal: TradeProposal,
    *,
    quantity: Decimal,
    entry_price: Decimal | None = None,
    exchange_take_profit_enabled: bool = True,
) -> OrderRequest:
    if proposal.action is Action.LONG:
        side, position_side, reduce_only = "BUY", "LONG", False
    elif proposal.action is Action.SHORT:
        side, position_side, reduce_only = "SELL", "SHORT", False
    elif proposal.action is Action.CLOSE:
        side = proposal.side or "SELL"
        position_side = "LONG" if side.upper() == "SELL" else "SHORT"
        reduce_only = True
    else:
        raise ValueError("HOLD proposals do not create orders")
    # 交易所挂的是宽灾难止损（3×），不是软件层那条紧的 —— 触发单不能用于移动止损。
    # 静态 TP 也在开仓时附带，之后由本地管理器继续负责动态退出。没给灾难止损时退回 stop_loss。
    exchange_stop = proposal.disaster_stop if proposal.disaster_stop is not None else proposal.stop_loss
    exchange_take_profit = (
        Decimal(str(proposal.take_profit))
        if exchange_take_profit_enabled
        and proposal.action in {Action.LONG, Action.SHORT}
        and proposal.take_profit is not None
        else None
    )
    return OrderRequest(
        symbol=proposal.symbol,
        side=side,
        position_side=position_side,
        order_type="MARKET",
        quantity=quantity,
        client_order_id=stable_client_order_id(proposal.proposal_id),
        price=entry_price,
        stop_loss=Decimal(str(exchange_stop)) if exchange_stop is not None else None,
        # 静态 TP 由交易所优先触发，PositionManager 仍保留为延迟/触发失败时的兜底。
        # 平仓请求不附带 TP，避免把已关闭仓位的退出参数误传给交易所。
        take_profit=exchange_take_profit,
        reduce_only=reduce_only,
    )


def _query_by_client_id(exchange: Any, client_order_id: str) -> ExchangeOrder | None:
    query = getattr(exchange, "get_order_by_client_id", None)
    if query is None:
        query = getattr(exchange, "get_order", None)
    if query is None:
        return None
    try:
        return query(client_order_id)
    except Exception:  # noqa: BLE001 - query failure means status is unknown
        return None


class ExecutionService:
    def __init__(self, *, exchange_take_profit_enabled: bool = True) -> None:
        self.exchange_take_profit_enabled = exchange_take_profit_enabled

    def execute(
        self,
        exchange: ExchangeClient,
        proposal: TradeProposal,
        risk_decision: RiskDecision,
        *,
        quantity: Decimal,
        entry_price: Decimal | None = None,
    ) -> ExecutionResult:
        client_order_id = stable_client_order_id(proposal.proposal_id)
        if proposal.action is Action.HOLD:
            return ExecutionResult(
                status="SKIPPED",
                proposal_id=proposal.proposal_id,
                client_order_id=client_order_id,
                message="HOLD proposal does not create an order",
            )
        if not risk_decision.allowed:
            return ExecutionResult(
                status="REJECTED",
                proposal_id=proposal.proposal_id,
                client_order_id=client_order_id,
                message="risk decision did not allow execution",
            )
        request = _order_request_for(
            proposal,
            quantity=quantity,
            entry_price=entry_price,
            exchange_take_profit_enabled=self.exchange_take_profit_enabled,
        )
        try:
            order = exchange.place_order(request)
        except (TimeoutError, httpx.TimeoutException) as exc:
            existing = _query_by_client_id(exchange, client_order_id)
            if existing is not None:
                return self._result(proposal, client_order_id, existing, "timeout_reconciled")
            return ExecutionResult(
                status="UNKNOWN",
                proposal_id=proposal.proposal_id,
                client_order_id=client_order_id,
                message=f"order timeout; reconciliation required: {exc}",
            )
        except ExchangeError as exc:
            return ExecutionResult(
                status="REJECTED",
                proposal_id=proposal.proposal_id,
                client_order_id=client_order_id,
                message=str(exc),
            )
        return self._result(proposal, client_order_id, order)

    @staticmethod
    def _result(
        proposal: TradeProposal,
        client_order_id: str,
        order: ExchangeOrder,
        message: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            status=order.status,
            proposal_id=proposal.proposal_id,
            client_order_id=client_order_id,
            exchange_order_id=order.order_id,
            # 0 表示交易所没回报成交均价，等同于不可得。
            average_price=order.average_price if order.average_price > 0 else None,
            message=message,
        )


def execute_idempotently(
    exchange: ExchangeClient,
    *,
    proposal_id: str,
    proposal: TradeProposal | None = None,
    risk_decision: RiskDecision | None = None,
    quantity: Decimal = Decimal("0.01"),
) -> ExecutionResult:
    """Compatibility helper for one-off workers and contract tests."""

    actual_proposal = proposal or TradeProposal(
        proposal_id=proposal_id,
        action=Action.LONG,
        symbol="BTC-USDT",
        side="BUY",
        position_size_pct=0.01,
        leverage=1,
        stop_loss=1,
        take_profit=None,
        valid_until=9_999_999_999_999,
        confidence=1,
        reasoning_summary="test or worker proposal",
        evidence_refs=["worker"],
        model_version="worker",
        trace_id="worker",
    )
    actual_risk = risk_decision or RiskDecision(status=RiskStatus.ALLOWED)
    return ExecutionService().execute(exchange, actual_proposal, actual_risk, quantity=quantity)

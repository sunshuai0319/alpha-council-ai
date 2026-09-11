from decimal import Decimal

from app.domain.enums import Action
from app.domain.schemas import RiskDecision, TradeProposal
from app.exchange.base import ExchangeOrder
from app.execution.service import ExecutionService, execute_idempotently, stable_client_order_id


def _order(client_order_id: str) -> ExchangeOrder:
    return ExchangeOrder(
        order_id="exchange-1",
        client_order_id=client_order_id,
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
        created_at=None,
        updated_at=None,
    )


class TimeoutExchange:
    def __init__(self, existing: ExchangeOrder | None = None) -> None:
        self.place_order_calls = 0
        self.query_order_calls = 0
        self.existing = existing

    def place_order(self, request: object) -> ExchangeOrder:
        del request
        self.place_order_calls += 1
        raise TimeoutError("simulated timeout")

    def get_order_by_client_id(self, client_order_id: str) -> ExchangeOrder | None:
        del client_order_id
        self.query_order_calls += 1
        return self.existing


def test_order_timeout_queries_before_retry() -> None:
    fake_exchange = TimeoutExchange()
    result = execute_idempotently(fake_exchange, proposal_id="p-1")
    assert fake_exchange.place_order_calls == 1
    assert fake_exchange.query_order_calls == 1
    assert result.status == "UNKNOWN"


def test_timeout_with_existing_order_is_reconciled_without_duplicate() -> None:
    client_order_id = stable_client_order_id("p-2")
    fake_exchange = TimeoutExchange(existing=_order(client_order_id))
    result = execute_idempotently(fake_exchange, proposal_id="p-2")
    assert fake_exchange.place_order_calls == 1
    assert fake_exchange.query_order_calls == 1
    assert result.status == "FILLED"


def test_rejected_risk_never_calls_exchange() -> None:
    class FailingExchange:
        def place_order(self, request: object) -> ExchangeOrder:
            raise AssertionError("risk rejection must not call exchange")

    proposal = TradeProposal(
        proposal_id="p-3",
        action=Action.LONG,
        symbol="BTC-USDT",
        side="BUY",
        position_size_pct=0.1,
        leverage=2,
        stop_loss=95,
        valid_until=9_999_999_999_999,
        confidence=0.8,
        reasoning_summary="test",
        evidence_refs=["e1"],
        model_version="test",
        trace_id="t1",
    )
    result = ExecutionService().execute(
        FailingExchange(), proposal, RiskDecision(status="REJECTED", reasons=["max_notional"]), quantity=Decimal("0.01")
    )
    assert result.status == "REJECTED"


class CapturingExchange:
    """记下下单请求，回报 FILLED。"""

    def __init__(self) -> None:
        self.request = None

    def place_order(self, request):
        self.request = request
        return _order(request.client_order_id)


def _entry_proposal(**overrides) -> TradeProposal:
    base = {
        "proposal_id": "p-4",
        "action": Action.SHORT,
        "symbol": "BTC-USDT",
        "side": "SHORT",
        "position_size_pct": 0.1,
        "leverage": 1,
        "stop_loss": 103,  # 软件层的紧止损 = 1R
        "take_profit": 94,
        "valid_until": 9_999_999_999_999,
        "confidence": 0.6,
        "reasoning_summary": "test",
        "evidence_refs": ["e1"],
        "model_version": "test",
        "trace_id": "t1",
    }
    return TradeProposal(**{**base, **overrides})


def test_exchange_stop_uses_the_wide_disaster_level_not_the_software_stop() -> None:
    """交易所挂的是 3× 灾难止损，不是软件层的 1× —— 那条改不了也撤不掉，
    挂紧的话移动止损根本没机会执行（实测无 cancel/modify 端点）。"""
    exchange = CapturingExchange()
    proposal = _entry_proposal(disaster_stop=109)

    ExecutionService().execute(
        exchange, proposal, RiskDecision(status="ALLOWED"), quantity=Decimal("0.01")
    )

    assert exchange.request.stop_loss == Decimal("109")


def test_falls_back_to_the_software_stop_when_no_disaster_level_is_given() -> None:
    exchange = CapturingExchange()
    proposal = _entry_proposal()  # 没带 disaster_stop

    ExecutionService().execute(
        exchange, proposal, RiskDecision(status="ALLOWED"), quantity=Decimal("0.01")
    )

    assert exchange.request.stop_loss == Decimal("103")

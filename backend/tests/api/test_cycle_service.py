from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import (
    AccountSnapshot,
    Base,
    RiskEvent,
    TradingAccount,
    TradingDecision,
    User,
)
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import (
    Candle,
    ExecutionResult,
    MarketSnapshot,
    RiskDecision,
    TradeProposal,
    TradingCycleState,
)
from app.exchange.base import ExchangeBalance, ExchangeOrder, ExchangePosition, OrderRequest
from app.services.cycle import TradingCycleService


class FakeExchange:
    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        del limit
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=1_700_000_000_000,
                open=100,
                high=101,
                low=99,
                close=100,
                volume=10,
            )
        ]

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            captured_at=int(datetime.now(UTC).timestamp() * 1000),
            last_price=100,
        )

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", Decimal(10000), Decimal(10000), Decimal(0), Decimal(0))]

    def get_positions(self) -> list[object]:
        return []

    def place_order(self, request: object) -> object:
        raise AssertionError("invalid committee output must not place an order")


class FailingLLM:
    def complete_json(self, prompt: str) -> str:
        del prompt
        raise TimeoutError("LLM unavailable")


def test_cycle_failure_persists_hold_decision() -> None:
    service = TradingCycleService(exchange_factory=FakeExchange)
    result = service.run(user_id="u-1", llm=FailingLLM())
    assert result.action == "HOLD"
    assert result.persisted is True


class RecordingExchange:
    """记录下单请求并回报 FILLED。"""

    def __init__(self, balance: Decimal = Decimal(10000)) -> None:
        self.requests: list[OrderRequest] = []
        self._balance = balance

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", self._balance, self._balance, Decimal(0), Decimal(0))]

    def get_positions(self) -> list[object]:
        return []

    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        self.requests.append(request)
        return ExchangeOrder(
            order_id="order-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            position_side=request.position_side,
            status="FILLED",
            order_type=request.order_type,
            quantity=request.quantity,
            executed_quantity=request.quantity,
            price=Decimal(100),
            average_price=Decimal(100),
            time_in_force=None,
            created_at=None,
            updated_at=None,
        )


def _long_state(*, price: float = 100, pct: float = 0.1) -> TradingCycleState:
    return TradingCycleState(
        user_id="u-1",
        cycle_id="cycle-1",
        started_at=1,
        symbol="BTC-USDT",
        market_snapshot=MarketSnapshot(
            symbol="BTC-USDT", captured_at=int(datetime.now(UTC).timestamp() * 1000), last_price=price
        ),
        trade_proposal=TradeProposal(
            proposal_id="proposal-1",
            action=Action.LONG,
            symbol="BTC-USDT",
            side="BUY",
            position_size_pct=pct,
            leverage=1,
            stop_loss=price * 0.9,
            valid_until=9_999_999_999_999,
            confidence=0.8,
            reasoning_summary="test",
            evidence_refs=["evidence-1"],
            model_version="test",
            trace_id="trace-1",
        ),
    )


def test_persisting_a_filled_order_stores_json_safe_values(tmp_path) -> None:
    """成交回报带 Decimal，而 execution_result 是 JSON 列。

    只有订单真正成交时才会走到这里 —— 新鲜度门曾经把订单全拦掉，掩盖了它。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'persist.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db)

        service._persist(
            result_state=_close_state(),
            risk=RiskDecision(status=RiskStatus.ALLOWED),
            execution=ExecutionResult(
                status="FILLED",
                proposal_id="proposal-1",
                client_order_id="alpha-1",
                exchange_order_id="order-1",
                average_price=Decimal("100.5"),
                realized_pnl=Decimal("-1.25"),
            ),
        )

        row = db.scalars(select(TradingDecision)).all()[0]
        assert row.execution_result["realized_pnl"] == "-1.25"
        assert row.execution_result["average_price"] == "100.5"


def test_persisting_a_filled_order_round_trips_into_the_loss_streak(tmp_path) -> None:
    """写进去的盈亏要能被连亏统计读出来（Decimal -> JSON -> Decimal）。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'roundtrip.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db)

        for _ in range(3):
            service._persist(
                result_state=_close_state(),
                risk=RiskDecision(status=RiskStatus.ALLOWED),
                execution=ExecutionResult(
                    status="FILLED",
                    proposal_id="proposal-1",
                    client_order_id="alpha-1",
                    average_price=Decimal(90),
                    realized_pnl=Decimal(-10),
                ),
            )

        assert service._consecutive_losses("u-1") == 3


def test_circuit_breaker_pauses_the_account(tmp_path) -> None:
    """账户级熔断必须真的暂停账户，而不只是拒掉这一单。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'halt.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        service = TradingCycleService(db=db)
        assert service.control_status("u-1") == "RUNNING"

        service._halt(
            "u-1",
            RiskDecision(status=RiskStatus.REJECTED, reasons=["daily_loss_limit"], halt=True),
        )

        assert service.control_status("u-1") == "PAUSED"
        events = db.scalars(select(RiskEvent)).all()
        assert [event.event_type for event in events] == ["CIRCUIT_BREAKER"]
        assert "daily_loss_limit" in events[0].reason


def test_daily_loss_pct_uses_todays_first_snapshot(tmp_path) -> None:
    """熔断输入必须来自真实权益，而不是写死的 0。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cycle.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        now = datetime.now(UTC)
        db.add(
            AccountSnapshot(
                user_id="u-1",
                trading_account_id="a-1",
                captured_at=now.replace(hour=0, minute=1),
                balance=Decimal(10000),
                available_margin=Decimal(10000),
                equity=Decimal(10000),
            )
        )
        db.add(
            AccountSnapshot(
                user_id="u-1",
                trading_account_id="a-1",
                captured_at=now,
                balance=Decimal(8000),
                available_margin=Decimal(8000),
                equity=Decimal(8000),
            )
        )
        db.commit()

        service = TradingCycleService(db=db)
        assert service._daily_loss_pct("u-1", Decimal(9500)) == Decimal("0.05")


class ClosingExchange:
    """持有一个多仓，平仓成交价由 exit_price 决定。"""

    def __init__(self, *, exit_price: Decimal, side: str = "LONG") -> None:
        self.requests: list[OrderRequest] = []
        self._exit_price = exit_price
        self._side = side

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", Decimal(10000), Decimal(10000), Decimal(0), Decimal(0))]

    def get_positions(self) -> list[ExchangePosition]:
        return [
            ExchangePosition(
                position_id="position-1",
                symbol="BTC-USDT",
                side=self._side,
                quantity=Decimal(1),
                entry_value=Decimal(100),  # 入场价 100
                margin=Decimal(10),
                leverage=1,
                unrealized_pnl=Decimal(0),
                liquidation_price=None,
            )
        ]

    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        self.requests.append(request)
        return ExchangeOrder(
            order_id="order-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            position_side=request.position_side,
            status="FILLED",
            order_type=request.order_type,
            quantity=request.quantity,
            executed_quantity=request.quantity,
            price=self._exit_price,
            average_price=self._exit_price,
            time_in_force=None,
            created_at=None,
            updated_at=None,
        )


def _close_state(*, side: str = "LONG") -> TradingCycleState:
    state = _long_state()
    proposal = state.trade_proposal.model_copy(
        update={"action": Action.CLOSE, "side": "SELL" if side == "LONG" else "BUY"}
    )
    return state.model_copy(update={"trade_proposal": proposal})


def test_close_records_realized_pnl_from_entry_and_fill_price() -> None:
    """平仓必须记录这一回合的真实盈亏，连亏熔断才有输入。"""
    winner = ClosingExchange(exit_price=Decimal(110))
    service = TradingCycleService(exchange_factory=lambda: winner)
    execution = service._execute(winner, _close_state(), RiskDecision(status=RiskStatus.ALLOWED))
    assert execution.realized_pnl == Decimal(10)

    loser = ClosingExchange(exit_price=Decimal(90))
    service = TradingCycleService(exchange_factory=lambda: loser)
    execution = service._execute(loser, _close_state(), RiskDecision(status=RiskStatus.ALLOWED))
    assert execution.realized_pnl == Decimal(-10)


def test_close_records_realized_pnl_for_short_positions() -> None:
    """空头方向相反：出场价低于入场价才是盈利。"""
    exchange = ClosingExchange(exit_price=Decimal(90), side="SHORT")
    service = TradingCycleService(exchange_factory=lambda: exchange)

    execution = service._execute(exchange, _close_state(side="SHORT"), RiskDecision(status=RiskStatus.ALLOWED))

    assert execution.realized_pnl == Decimal(10)


def test_consecutive_losses_counts_trailing_losing_closes(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'streak.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        base = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        for index, (action, pnl) in enumerate(
            [("CLOSE", "5"), ("CLOSE", "-1"), ("CLOSE", "-2")]
        ):
            db.add(
                TradingDecision(
                    id=f"d-{index}",
                    user_id="u-1",
                    cycle_id=f"c-{index}",
                    trace_id="t",
                    symbol="BTC-USDT",
                    action=action,
                    status="REJECTED",
                    execution_result={"realized_pnl": pnl},
                    created_at=base.replace(minute=index),
                )
            )
        db.commit()

        service = TradingCycleService(db=db)
        assert service._consecutive_losses("u-1") == 2


def test_consecutive_loss_breaker_actually_reaches_the_risk_engine(tmp_path) -> None:
    """连亏达到上限必须真的拒单。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'streak-breaker.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        base = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        for index in range(3):  # max_consecutive_losses 默认 3
            db.add(
                TradingDecision(
                    id=f"d-{index}",
                    user_id="u-1",
                    cycle_id=f"c-{index}",
                    trace_id="t",
                    symbol="BTC-USDT",
                    action="CLOSE",
                    status="REJECTED",
                    execution_result={"realized_pnl": "-1"},
                    created_at=base.replace(minute=index),
                )
            )
        db.commit()

        exchange = RecordingExchange()
        service = TradingCycleService(db=db, exchange_factory=lambda: exchange)

        decision = service._evaluate_proposal(exchange, _long_state())

        assert decision.allowed is False
        assert "consecutive_loss_cooldown" in decision.reasons


def test_daily_loss_breaker_actually_reaches_the_risk_engine(tmp_path) -> None:
    """日亏损超限必须真的拒单。

    ``_evaluate_proposal`` 以前把 daily_loss_pct 硬编码成 0，导致
    MAX_DAILY_LOSS_PCT 永远不触发；这条断言把它钉住。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'breaker.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.add(
            AccountSnapshot(
                user_id="u-1",
                trading_account_id="a-1",
                captured_at=datetime.now(UTC).replace(hour=0, minute=1),
                balance=Decimal(10000),
                available_margin=Decimal(10000),
                equity=Decimal(10000),
            )
        )
        db.commit()

        exchange = RecordingExchange(balance=Decimal(9000))  # 当日 -10%
        service = TradingCycleService(db=db, exchange_factory=lambda: exchange)

        decision = service._evaluate_proposal(exchange, _long_state())

        assert decision.allowed is False
        assert "daily_loss_limit" in decision.reasons


def test_entry_quantity_is_notional_over_price() -> None:
    """数量 = 名义价值 / 价格，名义价值 = 余额 × position_size_pct。

    只除以价格会丢掉余额因子：余额 10000 时下单量会小一万倍，远低于交易所
    最小下单量而被拒。
    """
    exchange = RecordingExchange(balance=Decimal(10000))
    service = TradingCycleService(exchange_factory=lambda: exchange)

    service._execute(exchange, _long_state(price=100, pct=0.1), RiskDecision(status=RiskStatus.ALLOWED))

    # 10000 × 0.1 = 1000 名义价值；1000 / 100 = 10
    assert exchange.requests[0].quantity == Decimal(10)

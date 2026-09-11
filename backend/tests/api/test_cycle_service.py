from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AccountSnapshot,
    Base,
    MarketMicrostructureRecord,
    RiskEvent,
    TradingAccount,
    TradingDecision,
    User,
)
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import (
    AnalysisResult,
    Candle,
    ExecutionResult,
    MarketMicrostructure,
    MarketSnapshot,
    RiskDecision,
    TradeProposal,
    TradingCycleState,
)
from app.exchange.base import (
    ExchangeBalance,
    ExchangeError,
    ExchangeOrder,
    ExchangePosition,
    OrderRequest,
)
from app.risk.engine import RiskLimits
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

    def get_microstructure(self, symbol: str) -> MarketMicrostructure:
        return MarketMicrostructure(
            symbol=symbol,
            captured_at=int(datetime.now(UTC).timestamp() * 1000),
            bid=99.9,
            ask=100.1,
            spread_bps=20.0,
            depth_imbalance=0.1,
            taker_buy_ratio=0.55,
            funding_rate=0.0001,
            open_interest=1000.0,
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


class RecordingCandleExchange(FakeExchange):
    """记录每轮请求的 K 线深度，用来断言采集没有退回浅历史。"""

    def __init__(self) -> None:
        self.candle_limits: list[int] = []

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        self.candle_limits.append(limit)
        return super().get_candles(symbol, timeframe, limit)


def _failure_service(tmp_path, name: str, **settings_kwargs) -> TradingCycleService:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="u-1", clerk_user_id="clerk-u1"))
    db.commit()
    return TradingCycleService(db=db, settings=Settings(**settings_kwargs))


def test_exchange_outage_does_not_pause_the_account(tmp_path) -> None:
    """对端 503 不是策略失败，不该把账户熔断。

    实测 WEEX 的 503 是间歇性的常规抖动（order/history 与 position/allPosition
    都出现过）。把它算进连亏熔断，会让一次对端抖动把账户 PAUSED 到需要人工恢复。
    """
    service = _failure_service(tmp_path, "outage.db", max_consecutive_failures=2)

    for _ in range(5):
        service.record_failure("u-1", "BTC-USDT", ExchangeError("WEEX request failed: 503"))

    assert service.control_status("u-1") == "RUNNING", "对端故障不应触发 pause"


def test_strategy_failures_still_pause_the_account(tmp_path) -> None:
    """外部故障豁免不能把真正的熔断也豁免掉。"""
    service = _failure_service(tmp_path, "strategy.db", max_consecutive_failures=2)

    for _ in range(3):
        service.record_failure("u-1", "BTC-USDT", RuntimeError("committee output exploded"))

    assert service.control_status("u-1") == "PAUSED"


def test_cycle_collects_the_deepest_history_the_api_allows() -> None:
    """历史深度直接决定能否回测。klines 无分页，单请求 1000 根就是上限。"""
    exchange = RecordingCandleExchange()
    service = TradingCycleService(exchange_factory=lambda: exchange)

    service.run(user_id="u-1", symbol="BTC-USDT", llm=FailingLLM())

    assert exchange.candle_limits == [1000, 1000, 1000], f"实际收到 {exchange.candle_limits}"


def test_cycle_persists_microstructure_without_affecting_the_decision(tmp_path) -> None:
    """微观结构只存不用：它的存在不应改变决策结果。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'micro.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = TradingCycleService(db=db, exchange_factory=FakeExchange)
        result = service.run(user_id="u-1", llm=FailingLLM())

        # 断言在 with 内：出去之后会话关闭，访问属性会 DetachedInstanceError
        rows = db.scalars(select(MarketMicrostructureRecord)).all()
        assert result.action == "HOLD"
        assert len(rows) == 1
        assert rows[0].symbol == "BTC-USDT"
        assert rows[0].funding_rate == 0.0001


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


def test_events_are_paginated_newest_first(tmp_path) -> None:
    """风控事件列表要分页，否则事件多了会一次全拉回来。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'events-paging.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        base = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        for index in range(5):
            db.add(
                RiskEvent(
                    id=f"e-{index}",
                    user_id="u-1",
                    event_type="RISK_GATE",
                    status=RiskStatus.ALLOWED,
                    reason=f"r-{index}",
                    created_at=base.replace(minute=index),
                )
            )
        db.commit()
        service = TradingCycleService(db=db)

        first = service.events("u-1", page=1, page_size=2)
        assert first["total"] == 5
        assert [item["id"] for item in first["items"]] == ["e-4", "e-3"]

        last = service.events("u-1", page=3, page_size=2)
        assert [item["id"] for item in last["items"]] == ["e-0"]


def test_decisions_are_paginated_newest_first(tmp_path) -> None:
    """决策列表要分页，否则账本增长后一次全拉回来。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'paging.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        base = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        for index in range(5):
            db.add(
                TradingDecision(
                    id=f"d-{index}",
                    user_id="u-1",
                    cycle_id=f"c-{index}",
                    trace_id="t",
                    symbol="BTC-USDT",
                    action="HOLD",
                    status="ALLOWED",
                    created_at=base.replace(minute=index),
                )
            )
        db.commit()
        service = TradingCycleService(db=db)

        first = service.decisions("u-1", page=1, page_size=2)
        assert first["total"] == 5
        assert [item["id"] for item in first["items"]] == ["d-4", "d-3"]

        last = service.decisions("u-1", page=3, page_size=2)
        assert [item["id"] for item in last["items"]] == ["d-0"]


def test_set_locale_persists_the_user_language(tmp_path) -> None:
    """界面语言偏好要落库，worker 才会按它生成对应语言的分析文本。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'locale.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-loc", clerk_user_id="clerk-loc"))
        db.commit()
        service = TradingCycleService(db=db)

        assert service.set_locale("u-loc", "en-US") == "en-US"
        assert db.get(User, "u-loc").locale == "en-US"
        assert service._user_locale("u-loc") == "en-US"


def test_decisions_return_analyses_and_account_leverage(tmp_path) -> None:
    """决策详情要能展示：agent 分析和账户实际杠杆（而非提案占位的 1x）。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'decisions.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db)
        state = _long_state().model_copy(
            update={
                "market_analysis": AnalysisResult(
                    status="neutral", confidence=0.4, reasoning_summary="market reasoning",
                    evidence_refs=[], model_version="m", trace_id="t1",
                ),
                "quant_analysis": AnalysisResult(
                    status="BEARISH", confidence=0.6, reasoning_summary="quant reasoning",
                    evidence_refs=[], model_version="q", trace_id="t2",
                ),
            }
        )
        service._persist(
            result_state=state,
            risk=RiskDecision(status=RiskStatus.ALLOWED, reasons=["hold_no_order"]),
            execution=None,
        )

        (item,) = service.decisions("u-1")["items"]
        assert item["analyses"]["market"]["reasoning_summary"] == "market reasoning"
        assert item["analyses"]["quant"]["status"] == "BEARISH"
        assert item["leverage"] == 20


def test_resume_sets_pending_immediate_and_consume_clears_it(tmp_path) -> None:
    """点「恢复周期」后 scheduler 应尽快跑一轮，而不是等满 5 分钟。

    resume 置 pending_immediate，consume 消费一次并复位；再 consume 为空。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pending.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-pending", clerk_user_id="clerk-pending"))
        db.commit()
        service = TradingCycleService(db=db)

        assert service.resume("u-pending")["status"] == "RUNNING"
        assert service.consume_pending_immediate() is True
        assert service.consume_pending_immediate() is False


def test_pause_clears_pending_immediate(tmp_path) -> None:
    """暂停后再恢复前不能有残留的立即执行标记。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pending-pause.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-pending", clerk_user_id="clerk-pending"))
        db.commit()
        service = TradingCycleService(db=db)

        service.resume("u-pending")
        service.pause("u-pending")
        assert service.consume_pending_immediate() is False


def test_persist_inserts_parent_decision_before_child_risk_event(tmp_path) -> None:
    """SessionLocal(autoflush=False) 下父子表同一次 commit 可能外键违例。

    生产 `_persist` 先 add(TradingDecision) 再 add(RiskEvent) 后一次 commit，
    两者之间没有 relationship() 时 SQLAlchemy 不保证插入顺序，risk_events
    可能先插入 → decision_id 外键违例，worker 崩溃。测试必须用 autoflush=False
    + PRAGMA foreign_keys=ON 模拟生产，默认的 Session(autoflush=True) 测不出。
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'fk-persist.db'}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", lambda raw_conn, _: raw_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        # FK 强制开启后 setup 也要先父后子分开提交，否则 TradingAccount 可能先插。
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db)

        service._persist(
            result_state=_long_state(),
            risk=RiskDecision(status=RiskStatus.ALLOWED, reasons=["hold_no_order"]),
            execution=None,
        )

        decision = db.scalars(select(TradingDecision)).all()[0]
        event_row = db.scalars(select(RiskEvent)).all()[0]
        assert event_row.decision_id == decision.id


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


class MultiPositionExchange(RecordingExchange):
    """同时持有 BTC 和 ETH 两个仓位。"""

    def __init__(self, btc_notional: Decimal, eth_notional: Decimal) -> None:
        super().__init__(balance=Decimal(10000))
        self._notionals = {"BTC-USDT": btc_notional, "ETH-USDT": eth_notional}

    def get_positions(self) -> list[ExchangePosition]:
        return [
            ExchangePosition(
                position_id=f"p-{symbol}",
                symbol=symbol,
                side="LONG",
                quantity=Decimal(1),
                entry_value=notional,
                margin=Decimal(1),
                leverage=1,
                unrealized_pnl=Decimal(0),
                liquidation_price=None,
            )
            for symbol, notional in self._notionals.items()
        ]


def test_notional_cap_counts_exposure_across_symbols() -> None:
    """敞口上限必须算账户总敞口。

    按品种各算 20%，两个品种就能到 40%。BTC 500 + ETH 2000 = 2500 已经超过
    equity 10000 的 20%，此时再开 BTC 必须被拒。
    """
    exchange = MultiPositionExchange(btc_notional=Decimal(500), eth_notional=Decimal(2000))
    service = TradingCycleService(exchange_factory=lambda: exchange)

    decision = service._evaluate_proposal(exchange, _long_state())

    assert "max_notional" in decision.reasons


def test_daily_trades_counts_only_filled_entries(tmp_path) -> None:
    """额度只算今天真正成交过的开仓单：没成交的开仓和平仓都不占额度。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trades.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db)

        filled = ExecutionResult(
            status="FILLED", proposal_id="p", client_order_id="c", exchange_order_id="order-1"
        )
        for state, execution in [
            (_long_state(), filled),  # 成交的开仓 -> 计数
            (_long_state(), filled),  # 成交的开仓 -> 计数
            (_long_state(), None),  # 没下单的开仓 -> 不计数
            (_close_state(), filled),  # 平仓 -> 不占额度
        ]:
            service._persist(
                result_state=state, risk=RiskDecision(status=RiskStatus.ALLOWED), execution=execution
            )

        assert service._daily_trades("u-1") == 2


def test_account_risk_limits_tighten_the_gate(tmp_path) -> None:
    """账户偏好生效：用户把仓位上限调小后，同样的提案会被拒。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'limits.db'}")
    Base.metadata.create_all(engine)

    settings = Settings(max_position_notional_pct=0.20, max_leverage=20)

    def decision_for(risk_limits):
        with Session(engine) as db:
            db.query(TradingAccount).delete()
            db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True, risk_limits=risk_limits))
            db.commit()
            exchange = RecordingExchange(balance=Decimal(10000))
            service = TradingCycleService(
                db=db, settings=settings, exchange_factory=lambda: exchange
            )
            return service._evaluate_proposal(exchange, _long_state(pct=0.1))

    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()

    # 平台上限 0.20：10% 的提案通过
    assert "max_notional" not in decision_for(None).reasons
    # 用户收紧到 0.05：同样的提案被拒
    assert "max_notional" in decision_for({"max_position_notional_pct": 0.05}).reasons


def test_global_kill_switch_stops_all_trading() -> None:
    """运维需要一键停掉所有账户的开关，而不是逐个暂停。"""
    service = TradingCycleService(
        settings=Settings(trading_enabled=False), exchange_factory=FakeExchange
    )

    result = service.run(user_id="u-1")

    assert result.action == "HOLD"
    assert result.risk_decision.status is RiskStatus.PAUSED
    assert result.execution_result is None  # FakeExchange 的下单会直接断言失败


def test_risk_uses_exchange_reported_leverage_over_the_proposal() -> None:
    """系统不下发杠杆，实际杠杆由账户决定（虚拟盘实测固定 20x）。

    校验提案里 LLM 自己填的数字没有意义：那个数字从来不参与下单。
    """
    exchange = ClosingExchange(exit_price=Decimal(100), leverage=20)  # 实际 20x
    # 显式钉住上限，避免测试结果随开发机 .env 变化
    service = TradingCycleService(
        settings=Settings(max_leverage=10), exchange_factory=lambda: exchange
    )

    decision = service._evaluate_proposal(exchange, _long_state())  # 提案声明 leverage=1

    assert "max_leverage" in decision.reasons


def test_risk_falls_back_to_the_proposal_when_leverage_is_unobservable() -> None:
    exchange = RecordingExchange()  # 空仓，没有真实杠杆可读
    service = TradingCycleService(
        settings=Settings(max_leverage=10), exchange_factory=lambda: exchange
    )

    decision = service._evaluate_proposal(exchange, _long_state())

    assert "max_leverage" not in decision.reasons


def test_virtual_account_leverage_fits_under_the_default_cap() -> None:
    """平台默认上限必须容纳虚拟盘固定的 20x，否则一开仓就再也加不了仓。"""
    assert RiskLimits().max_leverage >= 20
    assert Settings.model_fields["max_leverage"].default >= 20

    exchange = ClosingExchange(exit_price=Decimal(100), leverage=20)
    service = TradingCycleService(
        settings=Settings(max_leverage=20), exchange_factory=lambda: exchange
    )

    decision = service._evaluate_proposal(exchange, _long_state())

    assert "max_leverage" not in decision.reasons


def test_repeated_cycle_failures_pause_the_account(tmp_path) -> None:
    """连续失败必须停下来。

    之前每轮失败只写一条 RiskEvent 就继续：数据库挂掉会每 5 分钟撞一次，
    永远不停。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'failures.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        service = TradingCycleService(db=db, settings=Settings(max_consecutive_failures=3))

        service.record_failure("u-1", "BTC-USDT", RuntimeError("boom 1"))
        service.record_failure("u-1", "BTC-USDT", RuntimeError("boom 2"))
        assert service.control_status("u-1") == "RUNNING"

        service.record_failure("u-1", "BTC-USDT", RuntimeError("boom 3"))
        assert service.control_status("u-1") == "PAUSED"


def test_a_successful_cycle_resets_the_failure_streak(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reset.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db, settings=Settings(max_consecutive_failures=2))

        service.record_failure("u-1", "BTC-USDT", RuntimeError("boom 1"))
        service._persist(
            result_state=_long_state(),
            risk=RiskDecision(status=RiskStatus.ALLOWED),
            execution=None,
        )
        service.record_failure("u-1", "BTC-USDT", RuntimeError("boom 2"))

        assert service.control_status("u-1") == "RUNNING"
        assert service._consecutive_failures("u-1") == 1


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

    def __init__(self, *, exit_price: Decimal, side: str = "LONG", leverage: int = 1) -> None:
        self.requests: list[OrderRequest] = []
        self._exit_price = exit_price
        self._side = side
        self._leverage = leverage

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
                leverage=self._leverage,
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

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AccountSnapshot,
    Base,
    CollectorError,
    MarketCandle,
    MarketMicrostructureRecord,
    PnlSnapshot,
    Position,
    RiskEvent,
    TradingAccount,
    TradingDecision,
    User,
)
from app.db.models import (
    MarketSnapshot as MarketSnapshotModel,
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
    #: 虚拟盘没有成交流水。声明出来，对账才会走「余额差分」而不是去调 get_trades
    #: —— 少了这一行，每次对账都会抛 AttributeError（实测被 best-effort 吞成 warning，
    #: 账户同步静默失效）。
    supports_trade_fills = False

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


def test_market_candle_persistence_checks_existing_rows_in_bulk(tmp_path) -> None:
    """远程 PostgreSQL 上不能为每根 K 线单独发一次存在性查询。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'bulk-candles.db'}")
    Base.metadata.create_all(engine)
    timestamps = (1_700_000_000_000, 1_700_043_200_000, 1_700_086_400_000)
    candles = [
        Candle(
            symbol="BTC-USDT",
            timeframe="12h",
            open_time=timestamp,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=10,
        )
        for timestamp in (*timestamps, timestamps[0])
    ]
    statements: list[str] = []

    with Session(engine) as db:
        db.add(
            MarketCandle(
                symbol="BTC-USDT",
                timeframe="12h",
                open_time=timestamps[0],
                open=100,
                high=101,
                low=99,
                close=100,
                volume=10,
            )
        )
        db.commit()

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with Session(engine) as db:
            service = TradingCycleService(db=db)
            service._persist_market_data(candles, [])
            assert len(db.scalars(select(MarketCandle)).all()) == len(timestamps)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    select_count = sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    # 一次是持久化前的批量存在性检查，一次是测试本身的结果查询。
    assert select_count == 2


def test_snapshot_borrows_fields_the_ticker_omits_from_microstructure(tmp_path) -> None:
    """ticker 不返回买卖一/资金费率/持仓量，快照行要从同一轮的微观结构补上。

    否则 market_snapshots 这几列永远是空 —— 真值一直躺在
    market_microstructures 里，市场页的买一/卖一与资金费率只能显示占位符。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'funding.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = TradingCycleService(db=db, exchange_factory=FakeExchange)
        service.run(user_id="u-1", llm=FailingLLM())

        rows = db.scalars(select(MarketSnapshotModel)).all()
        assert len(rows) == 1
        assert rows[0].bid == Decimal("99.9")
        assert rows[0].ask == Decimal("100.1")
        assert rows[0].funding_rate == 0.0001
        assert rows[0].open_interest == 1000.0


class PartiallyFailingExchange(FakeExchange):
    """只有一个周期抓不到，其余正常 —— 复现 2026-09-11 那次 12h 抓取超时。"""

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        if timeframe == "12h":
            raise ExchangeError(
                "WEEX request failed: GET /capi/v3/market/klines: "
                "_ssl.c:1011: The handshake operation timed out"
            )
        return super().get_candles(symbol, timeframe, limit)


def test_a_market_fetch_failure_is_stored_as_a_code_not_an_exception(tmp_path) -> None:
    """决策记录是要渲染到界面上的 —— 它只能存稳定码，不能存英文异常。

    原文不丢：它照旧进日志，并归并进 collector_errors 表供排查。
    """

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'codes.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.commit()
        service = TradingCycleService(
            db=db,
            settings=Settings(market_timeframes="12h,1d"),
            exchange_factory=PartiallyFailingExchange,
        )
        service.run(user_id="u-1", llm=FailingLLM())

        decision = db.scalars(select(TradingDecision)).one()
        assert decision.proposal["reasoning_summary"] == "market_data_unavailable:BTC-USDT/12h"

        # 原文没有丢，只是换了个地方待着。
        errors = db.scalars(select(CollectorError)).all()
        assert len(errors) == 1
        assert errors[0].collector == "weex-candles"
        assert "_ssl.c:1011" in errors[0].message


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
            # 零名义 = 没有仓位。不这样过滤的话，「单品种一仓」会把一个空仓
            # 也算成已开仓，测敞口时就永远到不了 max_notional。
            if notional > 0
        ]


def test_notional_cap_counts_exposure_across_symbols() -> None:
    """总敞口上限必须算账户总敞口（跨品种合计）。

    ETH 已有 5500，再开 BTC 1000 → 6500 超过 equity 10000 的 60%（总上限），
    必须被拒。持仓放在 ETH 上是为了不触发「单品种一仓」，让这条走到
    max_notional 分支。总上限从 20% 放宽到 60% 是为了能同时持多个品种。
    """
    exchange = MultiPositionExchange(btc_notional=Decimal(0), eth_notional=Decimal(5500))
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

    # 平台单笔上限 0.20：10% 的提案通过
    assert "max_position_notional" not in decision_for(None).reasons
    # 用户收紧到 0.05：同样的 10% 提案超过单笔上限，被拒
    assert "max_position_notional" in decision_for({"max_position_notional_pct": 0.05}).reasons


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
    # 持仓换成 ETH：杠杆是从任意持仓读的，与被提案的品种无关；放在 BTC 上会被
    # 「单品种一仓」提前拦下，就测不到这条了。
    exchange = ClosingExchange(exit_price=Decimal(100), leverage=20, symbol="ETH-USDT")
    # 显式钉住上限，避免测试结果随开发机 .env 变化
    service = TradingCycleService(
        settings=Settings(max_leverage=10), exchange_factory=lambda exchange=exchange: exchange
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

    def __init__(
        self,
        *,
        exit_price: Decimal,
        side: str = "LONG",
        leverage: int = 1,
        symbol: str = "BTC-USDT",
    ) -> None:
        self.requests: list[OrderRequest] = []
        self._exit_price = exit_price
        self._side = side
        self._leverage = leverage
        # 允许换成别的品种：测杠杆/敞口等风控算法时需要「持仓品种 ≠ 提案品种」，
        # 否则会被「单品种一仓」提前拦下，测不到想测的分支。
        self._symbol = symbol

    def get_balances(self) -> list[ExchangeBalance]:
        return [ExchangeBalance("SUSDT", Decimal(10000), Decimal(10000), Decimal(0), Decimal(0))]

    def get_positions(self) -> list[ExchangePosition]:
        return [
            ExchangePosition(
                position_id="position-1",
                symbol=self._symbol,
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


class OnePositionExchange(RecordingExchange):
    """只持有一个 BTC 仓位，用来测「单品种一仓」。"""

    def __init__(self, side: str = "LONG", notional: Decimal = Decimal(100)) -> None:
        super().__init__(balance=Decimal(10000))
        self._side = side
        self._notional = notional

    def get_positions(self) -> list[ExchangePosition]:
        return [
            ExchangePosition(
                position_id="p-1",
                symbol="BTC-USDT",
                side=self._side,
                quantity=Decimal(1),
                entry_value=self._notional,
                margin=Decimal(1),
                leverage=1,
                unrealized_pnl=Decimal(0),
                liquidation_price=None,
            )
        ]


def _evaluate(tmp_path, exchange) -> RiskDecision:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'one.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db, exchange_factory=lambda: exchange)
        return service._evaluate_proposal(exchange, _long_state(pct=0.01))


def test_new_entry_is_rejected_while_a_position_is_open(tmp_path) -> None:
    """单品种一仓：已有该 symbol 的仓位时不再加仓。

    名义上限能间接挡住加仓，但那是间接的 —— 5 分钟一轮的连续小加仓会绕过它。
    """
    decision = _evaluate(tmp_path, OnePositionExchange(side="LONG", notional=Decimal(100)))

    assert "position_already_open" in decision.reasons


def test_opposite_direction_entry_is_also_blocked_while_position_is_open(tmp_path) -> None:
    """反向信号也不能直接反手开仓，必须先平 —— 否则会有两个方向的仓位。"""
    decision = _evaluate(tmp_path, OnePositionExchange(side="SHORT", notional=Decimal(100)))

    assert "position_already_open" in decision.reasons


def test_entry_passes_when_no_position_is_open(tmp_path) -> None:
    decision = _evaluate(tmp_path, RecordingExchange(balance=Decimal(10000)))

    assert "position_already_open" not in decision.reasons


def test_closing_is_never_blocked_by_the_one_position_rule(tmp_path) -> None:
    """平仓必须永远放行，否则仓位会被锁死。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'close.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        exchange = OnePositionExchange(side="LONG", notional=Decimal(100))
        service = TradingCycleService(db=db, exchange_factory=lambda: exchange)
        decision = service._evaluate_proposal(exchange, _close_state())

    assert "position_already_open" not in decision.reasons


def _open_position_row(**overrides):
    base = {
        "id": "pos-1",
        "user_id": "u-1",
        "trading_account_id": "a-1",
        "symbol": "BTC-USDT",
        "side": "LONG",
        "quantity": Decimal(1),
        "entry_price": Decimal(100),
        "stop_loss": Decimal(97),
        "effective_stop": Decimal(97),
        "peak_price": Decimal(100),
        "opened_at": datetime.now(UTC),
        "status": "OPEN",
    }
    return Position(**{**base, **overrides})


def _service_with_position(tmp_path, exchange, row=None):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'manage.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="u-1", clerk_user_id="clerk-u1"))
    db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
    db.add(row or _open_position_row())
    db.commit()
    return db, TradingCycleService(db=db, exchange_factory=lambda: exchange)


def test_cycle_closes_a_position_that_hits_its_effective_stop(tmp_path) -> None:
    """软件层止损触及 → 下 reduceOnly 平仓单。

    交易所侧只有 3× 的宽灾难止损，紧的那条只能由软件层执行。
    """
    exchange = OnePositionExchange(side="LONG", notional=Decimal(100))
    db, service = _service_with_position(tmp_path, exchange)

    service._manage_positions(exchange, _long_state(price=96))  # 现价 96 < 有效止损 97
    db.commit()

    assert len(exchange.requests) == 1
    assert exchange.requests[0].side == "SELL"  # 平多仓
    assert exchange.requests[0].reduce_only is True
    assert db.get(Position, "pos-1").status == "CLOSED"


def test_cycle_moves_the_stop_to_breakeven_and_persists_it(tmp_path) -> None:
    """浮盈到 1R → 有效止损上移入场价，并落库。"""
    exchange = OnePositionExchange(side="LONG", notional=Decimal(100))
    db, service = _service_with_position(tmp_path, exchange)

    service._manage_positions(exchange, _long_state(price=104))
    db.commit()

    assert exchange.requests == []  # 没平仓
    assert db.get(Position, "pos-1").effective_stop == Decimal(100)


def test_position_management_leaves_other_symbols_alone(tmp_path) -> None:
    """本轮只跑 state.symbol —— 拿 BTC 的信号分去管 ETH 仓位是错的。"""
    exchange = OnePositionExchange(side="LONG", notional=Decimal(100))
    db, service = _service_with_position(
        tmp_path, exchange, row=_open_position_row(symbol="ETH-USDT")
    )

    service._manage_positions(exchange, _long_state(price=96))
    db.commit()

    assert exchange.requests == []
    assert db.get(Position, "pos-1").status == "OPEN"


class PausedMarketExchange(FakeExchange):
    """行情可用，并按需返回一个持仓。用来测暂停期间的行为。"""

    #: 虚拟盘没有成交流水 —— 对账靠余额差推已实现盈亏，不走 fill。
    supports_trade_fills = False

    def __init__(self, *, price: float = 100.0, hold_position: bool = True) -> None:
        self.requests: list[OrderRequest] = []
        self._price = price
        self._hold = hold_position

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            captured_at=int(datetime.now(UTC).timestamp() * 1000),
            last_price=self._price,
        )

    def get_positions(self) -> list[ExchangePosition]:
        if not self._hold:
            return []  # 交易所已经平掉了（例如自动止损）
        return [
            ExchangePosition(
                position_id="p-1", symbol="BTC-USDT", side="LONG",
                quantity=Decimal(1), entry_value=Decimal(100), margin=Decimal(1),
                leverage=1, unrealized_pnl=Decimal(0), liquidation_price=None,
            )
        ]

    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        self.requests.append(request)
        # 成交之后仓位就没了 —— 桩件必须反映这一点，否则对账会把刚平掉的仓位
        # 又读回来、把本地行翻回 OPEN，测出一个现实中不存在的竞态。
        self._hold = False
        return ExchangeOrder(
            order_id="order-pm", client_order_id=request.client_order_id,
            symbol=request.symbol, side=request.side, position_side=request.position_side,
            status="FILLED", order_type=request.order_type, quantity=request.quantity,
            executed_quantity=request.quantity, price=Decimal(100), average_price=Decimal(100),
            time_in_force=None, created_at=None, updated_at=None,
        )


def test_paused_account_still_reconciles_the_phantom_row_away(tmp_path) -> None:
    """暂停只阻止新决策，不阻止观测。

    熔断恰恰发生在持亏损仓时；那时不对账，交易所自动止损后本地行会永远停在
    OPEN（就是第 1 层修过的那类幽灵持仓，从暂停这条路又能走到）。
    """
    exchange = PausedMarketExchange(hold_position=False)  # 交易所已无仓位
    db, service = _service_with_position(tmp_path, exchange)
    service.pause("u-1")

    service.run(user_id="u-1", llm=FailingLLM())

    assert db.get(Position, "pos-1").status == "CLOSED"


def test_paused_account_still_manages_an_open_position(tmp_path) -> None:
    """暂停期间持仓管理照跑 —— 放弃管理会让亏损走到 3× 的宽灾难止损。"""
    exchange = PausedMarketExchange(price=96.0)  # 现价 96 < 有效止损 97
    db, service = _service_with_position(tmp_path, exchange)
    service.pause("u-1")

    service.run(user_id="u-1", llm=FailingLLM())

    assert len(exchange.requests) == 1, "暂停期间止损仍应执行"
    assert exchange.requests[0].side == "SELL"
    assert db.get(Position, "pos-1").status == "CLOSED"


def test_a_second_symbol_can_be_held_within_the_total_cap() -> None:
    """这次改动的目的：同时持多个品种。

    单笔上限 20%、总上限 60% → 已有 20% + 新开 20% = 40% ≤ 60% → 放行。
    改动前总上限也是 20%，第二个品种必然被拒 —— 加品种等于白加。
    """
    exchange = MultiPositionExchange(btc_notional=Decimal(0), eth_notional=Decimal(2000))
    service = TradingCycleService(exchange_factory=lambda: exchange)

    decision = service._evaluate_proposal(exchange, _long_state(pct=0.2))

    assert "max_notional" not in decision.reasons, f"实际被拒: {decision.reasons}"
    assert "max_position_notional" not in decision.reasons


def test_portfolio_returns_only_open_positions(tmp_path) -> None:
    """`/api/portfolio` 语义是「当前持仓」，不能把已平仓的行也返回。

    实测踩到：本地只剩一行 status=CLOSED 的 BTC-USDT，接口照样返回它，
    于是 1) 持仓表显示一条已平仓的仓位；2) 概览的「持仓数」显示 1，而实际是 0。

    positions 表是「当前状态」表（同一 symbol 复用一行），已平仓的行不该出现在
    持仓里 —— 审计走决策历史。
    """
    db, service = _service_with_position(tmp_path, OnePositionExchange())
    service.db.commit()
    assert len(service.portfolio("u-1")["items"]) == 1

    # 平掉之后，接口不应再返回它
    db.get(Position, "pos-1").status = "CLOSED"
    service.db.commit()

    assert service.portfolio("u-1")["items"] == []


def _decision_rows(tmp_path, name: str):
    """建一个带若干决策的库，品种与动作混合。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="u-1", clerk_user_id="clerk-u1"))
    db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
    db.commit()
    base = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    rows = [
        ("BTC-USDT", "HOLD", 0),
        ("ETH-USDT", "HOLD", 1),
        ("BTC-USDT", "SHORT", 2),
        ("SOL-USDT", "HOLD", 3),
        ("BTC-USDT", "HOLD", 4),
    ]
    for index, (symbol, action, minute) in enumerate(rows):
        db.add(TradingDecision(
            id=f"d-{index}", user_id="u-1", cycle_id=f"c-{index}", trace_id="t",
            symbol=symbol, action=action, status="ALLOWED", created_at=base.replace(minute=minute),
        ))
    db.commit()
    return db, TradingCycleService(db=db)


def test_decisions_can_be_filtered_by_symbol(tmp_path) -> None:
    """决策列表按品种筛选 —— 十个品种之后不筛就没法看。"""
    _, service = _decision_rows(tmp_path, "filter-symbol.db")

    result = service.decisions("u-1", symbol="BTC-USDT")

    assert [item["symbol"] for item in result["items"]] == ["BTC-USDT"] * 3
    assert result["total"] == 3, "total 必须是过滤后的数量，否则分页页数会错"


def test_decisions_can_be_filtered_by_action(tmp_path) -> None:
    _, service = _decision_rows(tmp_path, "filter-action.db")

    result = service.decisions("u-1", action="SHORT")

    assert result["total"] == 1
    assert result["items"][0]["symbol"] == "BTC-USDT"


def test_decision_filters_combine(tmp_path) -> None:
    _, service = _decision_rows(tmp_path, "filter-both.db")

    result = service.decisions("u-1", symbol="BTC-USDT", action="HOLD")

    assert result["total"] == 2


def test_decisions_report_the_symbols_available_for_filtering(tmp_path) -> None:
    """筛选项要由数据驱动 —— 只列出真的出现过决策的品种。"""
    _, service = _decision_rows(tmp_path, "filter-list.db")

    result = service.decisions("u-1")

    assert result["symbols"] == ["BTC-USDT", "ETH-USDT", "SOL-USDT"]


class BrokerDownAfterDecision(FakeExchange):
    """行情可用，但账户类端点全部不可用（对端抖动时的真实形态）。"""

    def get_positions(self) -> list[object]:
        raise ExchangeError("WEEX request failed: GET /capi/v3/sim/position/allPosition: 503")

    def get_balances(self) -> list[ExchangeBalance]:
        raise ExchangeError("WEEX request failed: GET /capi/v3/sim/balance: 503")


def test_cycle_survives_an_account_endpoint_outage(tmp_path) -> None:
    """对账/持仓管理失败不能把整个周期炸掉。

    实测踩到：对端 503 时 reconcile 抛异常，而它在 _persist **之前** —— 于是这一轮
    的决策根本没落库，异常还冒到调度器记成 trading cycle failed。决策已经做完了，
    丢掉它比晚一轮同步仓位糟糕得多。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'outage.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="u-1", clerk_user_id="clerk-u1"))
    db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
    db.commit()
    service = TradingCycleService(db=db, exchange_factory=BrokerDownAfterDecision)

    result = service.run(user_id="u-1", symbol="BTC-USDT", llm=FailingLLM())

    assert result.action == "HOLD"
    assert result.persisted is True, "对端故障不该让决策丢失"
    rows = db.scalars(select(TradingDecision)).all()
    assert len(rows) == 1


def test_cycle_records_an_account_outage_without_halting(tmp_path) -> None:
    """故障要留痕（便于排查），但不能熔断账户。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'outage-events.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="u-1", clerk_user_id="clerk-u1"))
    db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
    db.commit()
    service = TradingCycleService(db=db, exchange_factory=BrokerDownAfterDecision)

    for _ in range(6):  # 超过 max_consecutive_failures
        service.run(user_id="u-1", symbol="BTC-USDT", llm=FailingLLM())

    assert service.control_status("u-1") == "RUNNING", "对端故障不该熔断账户"
    event_types = {row.event_type for row in db.scalars(select(RiskEvent)).all()}
    assert event_types, "故障必须留痕，否则排查时无从下手"


def test_account_state_syncs_once_per_cycle_not_once_per_symbol(tmp_path) -> None:
    """对账取的是**账户级**数据（positions / balances），与品种无关。

    十个品种各同步一次就是十份完全相同的快照：实测 account_snapshots 涨到 91 行
    （约 10 轮 × 10 品种），而权益曲线只需要每轮一个点。更要紧的是失败面被放大了
    十倍 —— 一个抖动端点会产生十次失败。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sync-once.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="u-1", clerk_user_id="clerk-u1"))
    db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
    db.commit()
    service = TradingCycleService(db=db, settings=Settings(), exchange_factory=FakeExchange)

    for symbol in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        service.run(user_id="u-1", symbol=symbol, llm=FailingLLM())

    assert len(db.scalars(select(AccountSnapshot)).all()) == 1, "一轮只该同步一次账户状态"
    assert len(db.scalars(select(PnlSnapshot)).all()) == 1
    # 但每个品种的决策都必须落库 —— 少同步不等于少决策
    assert len(db.scalars(select(TradingDecision)).all()) == 3


def test_account_sync_is_due_again_once_the_interval_passes(tmp_path) -> None:
    """跳过只是「本轮已同步」，不是永远不同步。

    窗口取决策间隔：下一轮开始时应当重新同步一次，否则账户状态会永远停在
    第一次观测上。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sync-window.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u-1", clerk_user_id="clerk-u1"))
        db.add(TradingAccount(id="a-1", user_id="u-1", enabled=True))
        db.commit()
        service = TradingCycleService(db=db, settings=Settings(), exchange_factory=FakeExchange)

        assert service._account_sync_due("a-1") is True, "还没同步过 → 该同步"

        db.add(AccountSnapshot(
            user_id="u-1", trading_account_id="a-1", captured_at=datetime.now(UTC),
            balance=Decimal(10000), available_margin=Decimal(10000), equity=Decimal(10000),
        ))
        db.commit()
        assert service._account_sync_due("a-1") is False, "刚同步过 → 本轮跳过"

        # 把那条快照挪到很久以前，模拟「上一轮」
        row = db.scalars(select(AccountSnapshot)).all()[0]
        row.captured_at = datetime.now(UTC) - timedelta(seconds=service.settings.decision_interval_seconds + 1)
        db.commit()
        assert service._account_sync_due("a-1") is True, "过了窗口 → 该重新同步"


def test_market_reports_the_freshness_rules_from_settings() -> None:
    """市场页的「数据时效规则」跟配置走，不能硬编码在页面里。

    页面曾写死 5m/1h/4h，而 MARKET_TIMEFRAMES 早已换成 12h/1d —— 前端拿不到
    配置，所以由 /market 把周期与新鲜度门一起下发。
    """
    service = TradingCycleService(
        settings=Settings(market_timeframes="12h,1d", market_data_max_age_seconds=90)
    )

    payload = service.market("u-1")

    assert payload["timeframes"] == ["12h", "1d"]
    assert payload["max_age_seconds"] == 90


def test_decisions_expose_the_model_versions() -> None:
    """智囊团 banner 的模型路由跟决策走，不能在文案里硬编码模型名。

    model_versions 早就存进了 trading_decisions（哪个模型做的这次决策），只是
    没从接口返回 —— 前端于是把 'deepseek-v4-pro-ga-260813' 写死在词条里，换模型
    就过时，连历史决策都会被显示成新模型。
    """
    row = TradingDecision(
        id="d-1",
        user_id="u-1",
        cycle_id="c-1",
        trace_id="t-1",
        symbol="BTC-USDT",
        action="HOLD",
        status="ALLOWED",
        model_versions={"committee": "deepseek-v4-pro-ga-260813"},
    )

    payload = TradingCycleService._decision_dict(row)

    assert payload["model_versions"] == {"committee": "deepseek-v4-pro-ga-260813"}

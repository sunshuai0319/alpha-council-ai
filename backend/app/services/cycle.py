import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph import TradingCycleGraph, build_trading_cycle_graph
from app.agents.llm import ArkChatClient
from app.analytics.indicators import INDICATOR_VERSION, calculate_indicators
from app.collectors.codes import market_data_error_code
from app.collectors.weex import WeexCollector
from app.config import Settings, get_settings
from app.db.collector_errors import record_collector_errors
from app.db.models import (
    AccountSnapshot,
    ControlState,
    MarketCandle,
    MarketMicrostructureRecord,
    Position,
    RiskEvent,
    TradingAccount,
    TradingDecision,
    User,
)
from app.db.models import MarketSnapshot as MarketSnapshotModel
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import (
    ExecutionResult,
    RiskDecision,
    TradeProposal,
    TradingCycleState,
)
from app.exchange.base import (
    ExchangeBalance,
    ExchangeClient,
    ExchangeError,
    ExchangePosition,
    OrderRequest,
)
from app.exchange.weex import WeexClient, WeexCredentials
from app.execution.service import ExecutionService, stable_client_order_id
from app.positions.manager import CLOSE as POSITION_CLOSE
from app.positions.manager import manage as manage_position
from app.rag.embeddings import BGEEmbedder, BGEReranker
from app.rag.milvus import MilvusVectorStore
from app.rag.retriever import Retriever
from app.reconciliation.service import ReconciliationService
from app.risk.engine import RiskEngine, daily_loss_pct
from app.services.context import load_macro_context
from app.signals.params import StrategyParams

logger = logging.getLogger(__name__)

#: 对端不可用（5xx / 超时 / 网络抖动）不是策略失败。把它算进连亏熔断，会让一次短暂
#: 的对端抖动把账户 PAUSED 到需要人工恢复 —— 实测 WEEX 的 503 是常规抖动。
EXTERNAL_FAILURE_TYPES = (ExchangeError, httpx.TimeoutException, httpx.TransportError)


def is_external_failure(error: BaseException) -> bool:
    """httpx 的异常常被包在 ExchangeError 里，所以也要看 __cause__。"""

    candidates = (error, error.__cause__)
    return any(isinstance(item, EXTERNAL_FAILURE_TYPES) for item in candidates if item is not None)


def round_trip_pnl(position: ExchangePosition, exit_price: Decimal) -> Decimal | None:
    """平仓回合的价差盈亏，**不含手续费**。

    手续费在下单响应里拿不到（虚拟盘更是完全没有成交流水），所以这里只算价差；
    口径偏宽松，会低估亏损。仓位数据不完整时返回 None，而不是猜一个 0。
    """

    if position.quantity == 0 or position.entry_value == 0:
        return None
    entry_price = abs(position.entry_value) / position.quantity
    direction = Decimal(-1) if position.side.upper() == "SHORT" else Decimal(1)
    return (exit_price - entry_price) * position.quantity * direction


class ControlRegistry:
    _states: ClassVar[dict[str, str]] = {}

    @classmethod
    def status(cls, user_id: str) -> str:
        return cls._states.get(user_id, "RUNNING")

    @classmethod
    def pause(cls, user_id: str) -> str:
        cls._states[user_id] = "PAUSED"
        return cls._states[user_id]

    @classmethod
    def resume(cls, user_id: str) -> str:
        cls._states[user_id] = "RUNNING"
        return cls._states[user_id]


@dataclass(frozen=True)
class CycleResult:
    state: TradingCycleState
    risk_decision: RiskDecision
    execution_result: ExecutionResult | None
    persisted: bool

    @property
    def action(self) -> Action:
        return self.state.trade_proposal.action if self.state.trade_proposal else Action.HOLD

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.state.cycle_id,
            "symbol": self.state.symbol,
            "action": self.action.value,
            "risk_status": self.risk_decision.status.value,
            "risk_reasons": self.risk_decision.reasons,
            "execution": self.execution_result.model_dump() if self.execution_result else None,
            "persisted": self.persisted,
        }


class TradingCycleService:
    def __init__(
        self,
        *,
        db: Session | None = None,
        settings: Settings | None = None,
        exchange_factory: Callable[[], ExchangeClient] | None = None,
        graph_factory: Callable[[], TradingCycleGraph] | None = None,
        risk_engine: RiskEngine | None = None,
        execution_service: ExecutionService | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.exchange_factory = exchange_factory
        self.graph_factory = graph_factory or self._default_graph
        self.risk_engine = risk_engine or RiskEngine(self.settings)
        self.execution_service = execution_service or ExecutionService()
        self.reconciliation = ReconciliationService(db=db)
        self._memory_results: dict[str, list[CycleResult]] = {}
        self._memory_market: list[dict[str, Any]] = []

    def _default_graph(self) -> TradingCycleGraph:
        llm = ArkChatClient(self.settings)
        vector_store = MilvusVectorStore(self.settings)
        retriever = Retriever(vector_store, BGEEmbedder(self.settings), BGEReranker(self.settings))
        return build_trading_cycle_graph(
            llm=llm,
            retriever=retriever,
            settings=self.settings,
        )

    def run(
        self,
        user_id: str,
        symbol: str = "BTC-USDT",
        llm: Any | None = None,
    ) -> CycleResult:
        cycle_id = str(uuid4())
        started_at = int(datetime.now(UTC).timestamp() * 1000)
        logger.info("cycle start: user=%s symbol=%s", user_id, symbol)
        exchange = self._exchange_for_user(user_id)
        collector = WeexCollector(exchange)
        timeframes = self.settings.timeframe_list
        candle_result, snapshot_result = collector.collect(
            symbols=(symbol,),
            timeframes=timeframes,
            # WEEX 的 klines 无分页且忽略 startTime/endTime，单请求 1000 根就是历史
            # 天花板。深度随周期而变：1h 只有 41 天，1d 有 999 天。
            limit=1000,
        )
        snapshot = snapshot_result.items[0] if snapshot_result.items else None
        # 微观结构采集失败的模式独立于 ticker。它不进打分卡（虚拟盘数值疑似合成），
        # 但 structure veto agent 与数据完整性检查用它判断流动性与盘口异常。
        microstructure_result = collector.collect_microstructures(symbols=(symbol,))
        microstructure = microstructure_result.items[0] if microstructure_result.items else None
        logger.info(
            "cycle market: user=%s symbol=%s price=%s candles=%d errors=%d",
            user_id,
            symbol,
            snapshot.last_price if snapshot else None,
            len(candle_result.items),
            len(candle_result.errors) + len(snapshot_result.errors),
        )
        # 原始异常只在这里（日志）与 collector_errors 表留痕 —— 决策记录里存的是
        # 稳定码。以前两头都没有：界面上是英文原文，库里也数不出行情错误率。
        for collected in (candle_result, snapshot_result, microstructure_result):
            for error in collected.errors:
                logger.warning("market collector error: user=%s symbol=%s %s", user_id, symbol, error)
        state = TradingCycleState(
            user_id=user_id,
            cycle_id=cycle_id,
            started_at=started_at,
            symbol=symbol,
            locale=self._user_locale(user_id),
            market_snapshot=snapshot,
            microstructure=microstructure,
            candles_by_timeframe={
                timeframe: [candle for candle in candle_result.items if candle.timeframe == timeframe]
                for timeframe in timeframes
            },
            technical_indicators=calculate_indicators(
                {
                    timeframe: [candle for candle in candle_result.items if candle.timeframe == timeframe]
                    for timeframe in timeframes
                }
            ),
            # 进决策记录（并最终渲染到界面）的是**稳定码**，不是原始异常：
            # 原文走日志与 collector_errors 表，界面只显示「哪个品种/周期没抓到」。
            # 见 app/collectors/codes.py。
            errors=[
                market_data_error_code(error)
                for collected in (candle_result, snapshot_result, microstructure_result)
                for error in collected.errors
            ],
            # 宏观事实只能从库里读（行情与账户事实不写进 RAG）。不传的话宏观 agent
            # 的上下文恒为空数组，只会一直报 insufficient_data。
            macro_events=load_macro_context(self.db, limit=20),
        )
        state = state.model_copy(update={"data_versions": {"technical_indicators": INDICATOR_VERSION}})
        self._persist_market_data(
            candle_result.items,
            snapshot_result.items,
            microstructure_result.items,
        )
        # 归并落库必须在 _persist 的 commit 之前（生产会话 autoflush=False）。
        record_collector_errors(self.db, candle_result, snapshot_result, microstructure_result)
        halt_reason = (
            None
            if self.control_status(user_id) != "PAUSED"
            else "paused"
        )
        if halt_reason is None and not self.settings.trading_enabled:
            halt_reason = "trading_disabled"
        # 持仓管理放在暂停判断**之前**：暂停只阻止新决策产生，不阻止给已有仓位降险。
        # 熔断恰恰由日亏损/连亏触发 —— 那时正持着亏损仓，放弃管理会让亏损一路走到
        # 交易所那条 3× 的宽灾难止损，而不是软件层的 1R。
        # 没有 state.signal_score 时结构失效不会触发（暂停期没有新信号），
        # 但止损触及、保本、移动、时间止损照常。
        #
        # 失败不终止周期：它同样是观测/降险步骤，对端抖动时把整轮炸掉只会丢掉决策。
        self._best_effort("position_management", user_id, symbol, lambda: self._manage_positions(exchange, state))
        if halt_reason is not None:
            logger.info(
                "cycle halted: user=%s symbol=%s reason=%s (no committee, no order)",
                user_id,
                symbol,
                halt_reason,
            )
            state.errors.append(halt_reason)
            state = state.model_copy(update={"trade_proposal": self._hold_proposal(state, halt_reason)})
            risk_decision = RiskDecision(status=RiskStatus.PAUSED, reasons=[halt_reason])
            execution = None
        else:
            # 信号器按风险预算反推仓位需要权益。把余额塞进 state 交给 signal_node
            # 读（graph 是无参 factory，不能在构造时传参）。_evaluate_proposal 里
            # 还会再取一次做风控，两次调用可接受。
            try:
                balance = self._balance_for(exchange)
            except Exception:  # noqa: BLE001 - 余额不可得时信号器按默认权益算，风控仍会把关
                balance = None
            equity = balance.balance if balance is not None else None
            if equity is not None:
                state = state.model_copy(update={"equity": equity})
            graph = (
                build_trading_cycle_graph(llm=llm, settings=self.settings)
                if llm is not None
                else self.graph_factory()
            )
            logger.info(
                "cycle signal: user=%s symbol=%s equity=%s (rule signal + LLM veto)",
                user_id,
                symbol,
                equity,
            )
            state = graph.invoke(state)
            proposal = state.trade_proposal
            logger.info(
                "cycle proposal: user=%s symbol=%s action=%s confidence=%s evidence=%d agents(market=%s,quant=%s,macro=%s)",
                user_id,
                symbol,
                proposal.action.value if proposal else "NONE",
                proposal.confidence if proposal else None,
                len(state.retrieved_evidence),
                state.market_analysis.status if state.market_analysis else None,
                state.quant_analysis.status if state.quant_analysis else None,
                state.macro_analysis.status if state.macro_analysis else None,
            )
            risk_decision = self._evaluate_proposal(exchange, state)
            if risk_decision.halt:
                self._halt(state.user_id, risk_decision)
            logger.info(
                "cycle risk: user=%s symbol=%s status=%s reasons=%s",
                user_id,
                symbol,
                risk_decision.status.value,
                risk_decision.reasons,
            )
            execution = self._execute(exchange, state, risk_decision)
            logger.info(
                "cycle execution: user=%s symbol=%s status=%s order_id=%s message=%s",
                user_id,
                symbol,
                execution.status if execution else "NO_ORDER",
                execution.exchange_order_id if execution else None,
                execution.message if execution else None,
            )
            state = state.model_copy(update={"risk_assessment": risk_decision, "execution_result": execution})
        # 对账同样与暂停无关：它是纯观测，也是「交易所自动止损后本地行怎么跟上」的
        # 唯一机制。放在暂停分支之外，暂停期间才不会留下幽灵持仓。
        account = self._account_for_user(user_id)
        if account is not None:
            # 有成交时**必须**对账：订单要回查、持仓行的元数据要在建行之后登记。
            # 没有成交时，对账就只剩「同步账户状态」这一件事 —— 而那是账户级数据，
            # 与品种无关。十个品种各做一次是十倍冗余（实测 account_snapshots 涨到
            # 91 行），失败面也放大十倍。所以一轮里只要已经同步过就跳过。
            order_ids = [execution.exchange_order_id] if execution and execution.exchange_order_id else []
            if not order_ids and not self._account_sync_due(account.id):
                persisted = self._persist(result_state=state, risk=risk_decision, execution=execution)
                return self._finish(state, risk_decision, execution, persisted, user_id, symbol)

            def _sync() -> None:
                self.reconciliation.reconcile(
                    exchange,
                    user_id=user_id,
                    trading_account_id=account.id,
                    order_ids=order_ids,
                    symbol=symbol,
                )
                # 必须在 reconcile 之后：行是它建的。把管理所需的元数据登记上去，
                # 否则下一轮 PositionManager 无从知道成本与止损在哪。
                self._register_position_metadata(user_id, account.id, symbol, state, execution)

            # 对账失败**绝不能**让周期失败：它在 _persist 之前，抛出去这一轮的决策就
            # 根本没落库（实测对端 503 时正是如此）。决策已经做完，丢掉它比晚一轮
            # 同步仓位糟糕得多 —— 下一轮对账会把落下的补上。
            self._best_effort("reconciliation", user_id, symbol, _sync)
        persisted = self._persist(result_state=state, risk=risk_decision, execution=execution)
        result = self._finish(state, risk_decision, execution, persisted, user_id, symbol)
        if snapshot:
            self._memory_market.append(snapshot.model_dump())
        return result

    def _finish(
        self,
        state: TradingCycleState,
        risk_decision: RiskDecision,
        execution: ExecutionResult | None,
        persisted: bool,
        user_id: str,
        symbol: str,
    ) -> CycleResult:
        result = CycleResult(state, risk_decision, execution, persisted)
        logger.info(
            "cycle done: user=%s symbol=%s action=%s risk=%s persisted=%s",
            user_id,
            symbol,
            result.action.value,
            risk_decision.status.value,
            persisted,
        )
        self._memory_results.setdefault(user_id, []).append(result)
        return result

    def _account_sync_due(self, trading_account_id: str) -> bool:
        """这个账户是否该做一次账户级同步。

        窗口取决策间隔：一轮里第一个品种同步、其余跳过；下一轮再同步一次。
        """

        if self.db is None:
            return True
        latest = self.db.scalar(
            select(func.max(AccountSnapshot.captured_at)).where(
                AccountSnapshot.trading_account_id == trading_account_id
            )
        )
        if latest is None:
            return True
        if latest.tzinfo is None:  # SQLite 取回来是 naive 的
            latest = latest.replace(tzinfo=UTC)
        return (datetime.now(UTC) - latest).total_seconds() >= self.settings.decision_interval_seconds

    @staticmethod
    def _hold_proposal(state: TradingCycleState, reason: str) -> TradeProposal:
        return TradeProposal(
            proposal_id=f"hold-{state.cycle_id}",
            action=Action.HOLD,
            symbol=state.symbol,
            position_size_pct=0,
            leverage=1,
            valid_until=state.started_at,
            invalidation_conditions=[reason],
            confidence=0,
            reasoning_summary=reason,
            model_version="safe-hold",
            trace_id=str(uuid4()),
        )

    def _evaluate_proposal(self, exchange: ExchangeClient, state: TradingCycleState) -> RiskDecision:
        proposal = state.trade_proposal
        if proposal is None:
            return RiskDecision(status=RiskStatus.REJECTED, reasons=["proposal_missing"])
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        # 以 signal_node 决策时刻为基准算行情年龄，而不是采集时刻 —— 否则 LLM
        # 否决耗时（最坏 = 重试 3 次 × 60s 超时）会被误算成行情过期。validate_freshness
        # 已经保证「信号决策时行情新鲜」，这里只需确认「从决策到下单没有拖太久」。
        market_age_base = state.signal_decided_at or (state.market_snapshot.captured_at if state.market_snapshot else now_ms)
        data_age = (now_ms - market_age_base) / 1000
        if proposal.action is Action.HOLD:
            return RiskDecision(status=RiskStatus.ALLOWED, reasons=["hold_no_order"], checked_at=now_ms)
        try:
            balance = self._balance_for(exchange)
            positions = exchange.get_positions()
        except Exception as exc:  # noqa: BLE001 - exchange outage fails closed
            return RiskDecision(status=RiskStatus.REJECTED, reasons=[f"account_unavailable:{exc}"], checked_at=now_ms)
        if balance is None or state.market_snapshot is None:
            return RiskDecision(status=RiskStatus.REJECTED, reasons=["account_or_market_missing"], checked_at=now_ms)
        # 单品种一仓：已有该 symbol 的仓位时不再开新仓，反向信号也要先平再说 ——
        # 否则 5 分钟一轮会连续加仓，两边的仓位还会同时存在。
        # 名义上限虽然也能挡住大部分加仓，但那是间接的（小单子会漏过去）。
        if proposal.action is not Action.CLOSE and any(
            position.symbol == proposal.symbol for position in positions
        ):
            return RiskDecision(status=RiskStatus.REJECTED, reasons=["position_already_open"], checked_at=now_ms)
        # 账户总敞口，跨品种合计：按品种各算一次上限，两个品种就能到两倍。
        current_notional = sum((abs(position.entry_value) for position in positions), Decimal(0))
        proposed_notional = balance.balance * Decimal(str(proposal.position_size_pct))
        is_reducing = proposal.action is Action.CLOSE
        current_equity = balance.balance + balance.unrealized_pnl
        # 系统不下发杠杆，实际杠杆由账户决定。有真实持仓时用交易所回报的杠杆，
        # 否则退回提案声明值 —— 空仓时无从观测，这一点无法回避。
        leverage = next(
            (position.leverage for position in positions if position.leverage > 0),
            proposal.leverage,
        )
        account = self._account_for_user(state.user_id)
        limits = self.risk_engine.limits.tightened(account.risk_limits if account else None)
        return self.risk_engine.evaluate(
            limits=limits,
            equity=balance.balance,
            current_notional=current_notional,
            proposed_notional=proposed_notional,
            leverage=leverage,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            entry=state.market_snapshot.last_price,
            daily_loss_pct=self._daily_loss_pct(state.user_id, current_equity),
            consecutive_losses=self._consecutive_losses(state.user_id),
            daily_trades=self._daily_trades(state.user_id),
            paused=False,
            data_age_s=data_age,
            side="LONG" if proposal.action is Action.LONG else "SHORT",
            is_reducing=is_reducing,
            checked_at=now_ms,
        )

    def _best_effort(
        self,
        step: str,
        user_id: str,
        symbol: str,
        action: Callable[[], None],
    ) -> None:
        """跑一个观测/降险步骤，失败只留痕，不终止周期。

        周期里除了「做决策」和「落库决策」，其余都是观测（对账、持仓管理）。
        它们失败时把整轮炸掉，代价是丢掉一个已经做完的决策 —— 而下一轮会把
        落下的补上。留痕用 record_failure，外部故障不会熔断账户（见第 1 层）。
        """

        try:
            action()
        except Exception as exc:  # noqa: BLE001 - 观测失败不该终止周期
            logger.warning("%s failed (cycle continues): user=%s symbol=%s error=%s", step, user_id, symbol, exc)
            try:
                self.record_failure(user_id, symbol, exc)
            except Exception:
                logger.exception("recording the %s failure also failed", step)

    def _manage_positions(self, exchange: ExchangeClient, state: TradingCycleState) -> None:
        """对**本品种**已有仓位跑持仓管理：触及有效止损/结构失效/时间止损就平，
        否则推进有效止损。

        只处理 state.symbol —— 拿 BTC 的信号分去管 ETH 的仓位是错的。

        平仓**不经过 RiskEngine**：风控里的 market_data_stale 等理由对减仓同样成立，
        走一遍就可能把一个止损平仓拦下来，把仓位锁死 —— 这是 spec 明确要避免的。
        """

        if self.db is None or state.market_snapshot is None:
            return
        account = self._account_for_user(state.user_id)
        if account is None:
            return
        price = Decimal(str(state.market_snapshot.last_price))
        entry_tf = StrategyParams.from_settings(self.settings).entry_timeframe
        atr_raw = ((state.technical_indicators or {}).get(entry_tf) or {}).get("atr_14")
        now = datetime.now(UTC)
        for position in exchange.get_positions():
            if position.symbol != state.symbol:
                continue
            row = self.db.scalar(
                select(Position).where(
                    Position.user_id == state.user_id,
                    Position.trading_account_id == account.id,
                    Position.symbol == position.symbol,
                    Position.status == "OPEN",
                )
            )
            if row is None:
                # 本地没有记录（例如人工开的仓）—— 不接管，免得瞎猜它的成本与止损。
                continue
            decision = manage_position(
                side=row.side,
                entry_price=row.entry_price,
                initial_stop=row.stop_loss or row.entry_price,
                effective_stop=row.effective_stop,
                peak_price=row.peak_price,
                price=price,
                atr=Decimal(str(atr_raw)) if atr_raw else None,
                opened_at=row.opened_at,
                now=now,
                signal_score=state.signal_score,
                params=StrategyParams.from_settings(self.settings),
            )
            if decision.action is POSITION_CLOSE:
                logger.info(
                    "position management closing: user=%s symbol=%s reason=%s",
                    state.user_id,
                    position.symbol,
                    decision.reason,
                )
                self._close_for_management(exchange, state, position, row, decision.reason)
                continue
            row.effective_stop = decision.effective_stop
            row.peak_price = decision.peak_price
        if self.db is not None:
            self.db.flush()

    def _close_for_management(
        self,
        exchange: ExchangeClient,
        state: TradingCycleState,
        position: ExchangePosition,
        row: Position,
        reason: str,
    ) -> None:
        """软件层止损/失效平仓。失败只记日志 —— 交易所侧还有宽灾难止损兜底。"""

        side = "SELL" if position.side == "LONG" else "BUY"
        request = OrderRequest(
            symbol=position.symbol,
            side=side,
            position_side=position.side,
            order_type="MARKET",
            quantity=position.quantity,
            # 同一个 cycle + 品种 → 同一个 id：本轮重试不会重复平仓。
            client_order_id=stable_client_order_id(f"{state.cycle_id}-{position.symbol}-close"),
            reduce_only=True,
        )
        try:
            order = exchange.place_order(request)
        except Exception as exc:  # noqa: BLE001 - 平仓失败下一轮重试，灾难止损兜底
            logger.warning(
                "position management close failed: user=%s symbol=%s reason=%s error=%s",
                state.user_id,
                position.symbol,
                reason,
                exc,
            )
            self.record_failure(state.user_id, position.symbol, exc)
            return
        row.status = "CLOSED"
        row.quantity = Decimal(0)
        row.unrealized_pnl = Decimal(0)
        row.stop_loss = None
        row.take_profit = None
        row.effective_stop = None
        logger.info(
            "position management closed: user=%s symbol=%s order=%s status=%s",
            state.user_id,
            position.symbol,
            order.order_id,
            order.status,
        )

    def _register_position_metadata(
        self,
        user_id: str,
        trading_account_id: str,
        symbol: str,
        state: TradingCycleState,
        execution: ExecutionResult | None,
    ) -> None:
        """开仓成交后，把初始止损 / 有效止损 / 开仓时刻登记到本地持仓行。

        持仓管理全靠这几个值：初始止损定义 1R，有效止损是软件层当前执行的那条。
        只在**开仓成交**时登记 —— 平仓/加仓不动它。
        """

        proposal = state.trade_proposal
        if (
            self.db is None
            or execution is None
            or execution.status != "FILLED"
            or proposal is None
            or proposal.action not in {Action.LONG, Action.SHORT}
        ):
            return
        row = self.db.scalar(
            select(Position).where(
                Position.user_id == user_id,
                Position.trading_account_id == trading_account_id,
                Position.symbol == symbol,
                Position.status == "OPEN",
            )
        )
        if row is None:
            return
        initial_stop = (
            Decimal(str(proposal.stop_loss)) if proposal.stop_loss is not None else row.entry_price
        )
        row.stop_loss = initial_stop
        row.effective_stop = initial_stop
        row.take_profit = (
            Decimal(str(proposal.take_profit)) if proposal.take_profit is not None else None
        )
        row.opened_at = datetime.now(UTC)
        row.peak_price = row.entry_price
        self.db.flush()

    def _halt(self, user_id: str, decision: RiskDecision) -> None:
        """账户级熔断：暂停该账户并留痕。

        暂停只阻止新决策产生；已有仓位不会被自动平掉 —— 自动强平太激进，
        交给操作者手动处理（控制台已有平仓入口）。
        """

        self.pause(user_id)
        logger.warning(
            "circuit breaker halted trading: user=%s reasons=%s", user_id, decision.reasons
        )
        if self.db is None:
            return
        self.db.add(
            RiskEvent(
                id=str(uuid4()),
                user_id=user_id,
                event_type="CIRCUIT_BREAKER",
                status=decision.status,
                reason=";".join(dict.fromkeys(decision.reasons)) or "halt",
                metadata_json=decision.metadata,
            )
        )
        self.db.commit()

    def _daily_trades(self, user_id: str) -> int:
        """今天真正成交过的开仓单数（CLOSE 不算额度）。"""

        if self.db is None:
            return 0
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            self.db.scalar(
                select(func.count())
                .select_from(TradingDecision)
                .where(
                    TradingDecision.user_id == user_id,
                    TradingDecision.created_at >= day_start,
                    TradingDecision.action.in_([Action.LONG.value, Action.SHORT.value]),
                    # 必须 as_string()：直接比 JSON 路径会被包成 JSON_QUOTE(...)，
                    # 而 JSON_QUOTE(NULL) 是字符串 'null'，IS NOT NULL 恒真。
                    TradingDecision.execution_result["exchange_order_id"].as_string().is_not(None),
                )
            )
            or 0
        )

    def _consecutive_losses(self, user_id: str) -> int:
        """最近连续亏损的平仓次数（跨品种，按账户计）。

        只扫到熔断阈值那么多条：够判断是否触发即可。没有 realized_pnl 的记录
        （平仓未成交）既不算亏损、也不打断此前连亏。
        """

        if self.db is None:
            return 0
        rows = self.db.scalars(
            select(TradingDecision.execution_result)
            .where(
                TradingDecision.user_id == user_id,
                TradingDecision.action == Action.CLOSE.value,
            )
            .order_by(TradingDecision.created_at.desc())
            .limit(self.settings.max_consecutive_losses)
        ).all()
        streak = 0
        for payload in rows:
            pnl = (payload or {}).get("realized_pnl")
            if pnl is None:
                continue
            if Decimal(str(pnl)) >= 0:
                break
            streak += 1
        return streak

    def _daily_loss_pct(self, user_id: str, current_equity: Decimal) -> Decimal:
        """当日权益回撤，基准是当天观测到的第一笔快照。

        系统当天首次启动时基准就是当时权益，因此早于系统启动的亏损不计入。
        """

        if self.db is None:
            return Decimal(0)
        account = self._account_for_user(user_id)
        if account is None:
            return Decimal(0)
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        day_open_equity = self.db.scalar(
            select(AccountSnapshot.equity)
            .where(
                AccountSnapshot.user_id == user_id,
                AccountSnapshot.trading_account_id == account.id,
                AccountSnapshot.captured_at >= day_start,
            )
            .order_by(AccountSnapshot.captured_at.asc(), AccountSnapshot.id.asc())
            .limit(1)
        )
        if day_open_equity is None:
            return Decimal(0)
        return daily_loss_pct(
            day_start_equity=day_open_equity,
            current_equity=current_equity,
        )

    def _execute(
        self,
        exchange: ExchangeClient,
        state: TradingCycleState,
        risk_decision: RiskDecision,
    ) -> ExecutionResult | None:
        proposal = state.trade_proposal
        if proposal is None or proposal.action is Action.HOLD:
            return None
        if state.market_snapshot is None or state.market_snapshot.last_price <= 0:
            return None
        position: ExchangePosition | None = None
        if proposal.action is Action.CLOSE:
            positions = exchange.get_positions()
            position = next((item for item in positions if item.symbol == proposal.symbol), None)
            quantity = position.quantity if position else Decimal(0)
        else:
            balance = self._balance_for(exchange)
            if balance is None:
                return ExecutionResult(
                    status="REJECTED",
                    proposal_id=proposal.proposal_id,
                    client_order_id=stable_client_order_id(proposal.proposal_id),
                    message="account balance unavailable",
                )
            # 名义价值 = 余额 × position_size_pct（与 _evaluate_proposal 同一口径），
            # 数量 = 名义价值 / 价格。漏掉余额因子会让下单量小 balance 倍。
            notional = balance.balance * Decimal(str(proposal.position_size_pct))
            quantity = notional / Decimal(str(state.market_snapshot.last_price))
        execution = self.execution_service.execute(exchange, proposal, risk_decision, quantity=quantity)
        if position is not None and execution.average_price is not None:
            pnl = round_trip_pnl(position, execution.average_price)
            if pnl is not None:
                execution = execution.model_copy(update={"realized_pnl": pnl})
        return execution

    @staticmethod
    def _balance_for(exchange: ExchangeClient) -> ExchangeBalance | None:
        return next(
            (item for item in exchange.get_balances() if item.asset in {"SUSDT", "USDT"}),
            None,
        )

    def enabled_user_ids(self) -> list[str]:
        if self.db is None:
            return []
        return list(
            self.db.scalars(
                select(TradingAccount.user_id).where(
                    TradingAccount.enabled.is_(True),
                    TradingAccount.provider == "weex",
                    TradingAccount.environment == "virtual",
                )
        ).all()
        )

    def _user_locale(self, user_id: str) -> str:
        """用户界面语言；worker 按它让 LLM 生成对应语言的分析文本。"""

        if self.db is None:
            return "zh-CN"
        user = self.db.get(User, user_id)
        return user.locale or "zh-CN" if user else "zh-CN"

    def set_locale(self, user_id: str, locale: str) -> str:
        """持久化界面语言偏好，供 worker 生成对应语言的分析文本。"""

        if self.db is None:
            return locale
        user = self.db.get(User, user_id)
        if user is not None:
            user.locale = locale
            self.db.commit()
        return locale

    def _account_for_user(self, user_id: str) -> TradingAccount | None:
        if self.db is None:
            return None
        return self.db.scalar(
            select(TradingAccount).where(
                TradingAccount.user_id == user_id,
                TradingAccount.provider == "weex",
                TradingAccount.environment == "virtual",
                TradingAccount.enabled.is_(True),
            )
        )

    def _exchange_for_user(self, user_id: str) -> ExchangeClient:
        if self.exchange_factory is not None:
            return self.exchange_factory()
        account = self._account_for_user(user_id)
        credentials = None
        if (
            account
            and account.api_key_ref
            and account.api_secret_ref
            and account.passphrase_ref
            and not any(
                value.startswith("env:")
                for value in (account.api_key_ref, account.api_secret_ref, account.passphrase_ref)
            )
        ):
            credentials = WeexCredentials(
                api_key=account.api_key_ref,
                api_secret=account.api_secret_ref,
                passphrase=account.passphrase_ref,
            )
        return WeexClient(self.settings, credentials=credentials)

    def _persist(
        self,
        *,
        result_state: TradingCycleState,
        risk: RiskDecision,
        execution: ExecutionResult | None,
    ) -> bool:
        if self.db is None:
            return True
        proposal = result_state.trade_proposal
        account = self._account_for_user(result_state.user_id)
        decision = TradingDecision(
            id=str(uuid4()),
            user_id=result_state.user_id,
            trading_account_id=account.id if account else None,
            cycle_id=result_state.cycle_id,
            trace_id=result_state.trace_ids[-1] if result_state.trace_ids else str(uuid4()),
            symbol=result_state.symbol,
            action=proposal.action.value if proposal else Action.HOLD.value,
            status=risk.status.value,
            proposal=proposal.model_dump() if proposal else None,
            analyses={
                "market": result_state.market_analysis.model_dump() if result_state.market_analysis else None,
                "quant": result_state.quant_analysis.model_dump() if result_state.quant_analysis else None,
                "macro": result_state.macro_analysis.model_dump() if result_state.macro_analysis else None,
                "technical_indicators": result_state.technical_indicators,
                # 每个专业 veto agent 的独立结论：记录它才能算各 agent 的否决精度
                # （拦掉的单子里多少事后看是对的）。
                "veto_verdicts": result_state.veto_verdicts or None,
            },
            risk_decision=risk.model_dump(mode="json"),
            # JSON 列：必须用 mode="json"，否则 Decimal（成交均价/已实现盈亏）存不进去。
            execution_result=execution.model_dump(mode="json") if execution else None,
            data_versions=result_state.data_versions,
            model_versions=result_state.model_versions,
            signal_score=result_state.signal_score,
            veto_type=result_state.veto_type,
        )
        self.db.add(decision)
        # SessionLocal 是 autoflush=False：RiskEvent 与 TradingDecision 之间只有
        # 裸外键、没有 relationship()，不 flush 的话同一次 commit 里子表可能先
        # 插入 → risk_events_decision_id_fkey 外键违例。父先落库即可解析子引用。
        self.db.flush()
        event_reasons = [*result_state.errors, *risk.reasons]
        if event_reasons:
            self.db.add(
                RiskEvent(
                    id=str(uuid4()),
                    user_id=result_state.user_id,
                    decision_id=decision.id,
                    event_type="CYCLE_SAFETY" if result_state.errors else "RISK_GATE",
                    status=risk.status,
                    reason=";".join(dict.fromkeys(event_reasons)),
                    metadata_json=risk.metadata,
                )
            )
        self.db.commit()
        return True

    def _persist_market_data(
        self,
        candles: list[Any],
        snapshots: list[Any],
        microstructures: list[Any] | None = None,
    ) -> None:
        if self.db is None:
            return
        captured_at = datetime.now(UTC)
        for candle in candles:
            existing = self.db.scalar(
                select(MarketCandle).where(
                    MarketCandle.symbol == candle.symbol,
                    MarketCandle.timeframe == candle.timeframe,
                    MarketCandle.open_time == candle.open_time,
                )
            )
            if existing is None:
                self.db.add(
                    MarketCandle(
                        symbol=candle.symbol,
                        timeframe=candle.timeframe,
                        open_time=candle.open_time,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                        source=candle.source,
                        captured_at=captured_at,
                    )
                )
        # ticker 不返回买卖一/资金费率/持仓量（见 docs/weex-virtual-api.md §1.2），
        # 真实值在同一轮的微观结构采集里。快照行从它补上 —— 否则 market_snapshots
        # 这几列永远是空，市场页的买一/卖一与资金费率只能显示占位符，而真值一直
        # 躺在 market_microstructures 里没人用。
        micro_by_symbol = {micro.symbol: micro for micro in (microstructures or [])}
        for snapshot in snapshots:
            micro = micro_by_symbol.get(snapshot.symbol)
            bid = snapshot.bid
            if bid is None and micro is not None:
                bid = micro.bid
            ask = snapshot.ask
            if ask is None and micro is not None:
                ask = micro.ask
            funding_rate = snapshot.funding_rate
            if funding_rate is None and micro is not None:
                funding_rate = micro.funding_rate
            open_interest = snapshot.open_interest
            if open_interest is None and micro is not None:
                open_interest = micro.open_interest
            self.db.add(
                MarketSnapshotModel(
                    symbol=snapshot.symbol,
                    captured_at=datetime.fromtimestamp(snapshot.captured_at / 1000, tz=UTC),
                    last_price=snapshot.last_price,
                    open_24h=snapshot.open_24h,
                    high_24h=snapshot.high_24h,
                    low_24h=snapshot.low_24h,
                    price_change_pct=snapshot.price_change_pct,
                    quote_volume_24h=snapshot.quote_volume_24h,
                    mark_price=snapshot.mark_price,
                    index_price=snapshot.index_price,
                    bid=bid,
                    ask=ask,
                    funding_rate=funding_rate,
                    open_interest=open_interest,
                    volume_24h=snapshot.volume_24h,
                )
            )
        for micro in microstructures or []:
            self.db.add(
                MarketMicrostructureRecord(
                    symbol=micro.symbol,
                    captured_at=datetime.fromtimestamp(micro.captured_at / 1000, tz=UTC),
                    bid=micro.bid,
                    ask=micro.ask,
                    spread_bps=micro.spread_bps,
                    depth_imbalance=micro.depth_imbalance,
                    taker_buy_ratio=micro.taker_buy_ratio,
                    funding_rate=micro.funding_rate,
                    open_interest=micro.open_interest,
                )
            )
        self.db.flush()

    def pause(self, user_id: str) -> dict[str, str]:
        return {"status": self._set_control_status(user_id, "PAUSED", pending_immediate=False)}

    def resume(self, user_id: str) -> dict[str, str]:
        # 用户主动恢复：置「立即执行」标记，scheduler 很快会消费它跑一轮，
        # 不必等满 decision_interval_seconds。
        return {"status": self._set_control_status(user_id, "RUNNING", pending_immediate=True)}

    def control_status(self, user_id: str) -> str:
        if self.db is None:
            return ControlRegistry.status(user_id)
        state = self.db.get(ControlState, user_id)
        return state.status if state else "RUNNING"

    def _set_control_status(
        self,
        user_id: str,
        status: str,
        *,
        pending_immediate: bool | None = None,
    ) -> str:
        if self.db is None:
            return ControlRegistry.pause(user_id) if status == "PAUSED" else ControlRegistry.resume(user_id)
        state = self.db.get(ControlState, user_id)
        if state is None:
            state = ControlState(user_id=user_id, status=status)
            self.db.add(state)
        else:
            state.status = status
            state.updated_at = datetime.now(UTC)
        if pending_immediate is not None:
            state.pending_immediate = pending_immediate
        self.db.commit()
        return status

    def consume_pending_immediate(self) -> bool:
        """消费「恢复周期」信号：有 RUNNING + pending 的账户就立即跑一轮。

        供 scheduler 短轮询调用；每次必须收掉事务，避免挂起只读事务阻塞 DDL
        （和 scheduler._end_transaction 同一道理）。
        """

        if self.db is None:
            return False
        rows = self.db.scalars(
            select(ControlState).where(
                ControlState.status == "RUNNING",
                ControlState.pending_immediate.is_(True),
            )
        ).all()
        if not rows:
            self.db.rollback()
            return False
        for row in rows:
            row.pending_immediate = False
        self.db.commit()
        return True

    def record_failure(self, user_id: str, symbol: str, error: Exception) -> None:
        if self.db is None:
            return
        external = is_external_failure(error)
        self.db.add(
            RiskEvent(
                id=str(uuid4()),
                user_id=user_id,
                # 事件类型就是熔断计数器的口径（见 _consecutive_failures），
                # 所以外部故障必须用另一个类型，否则等于没豁免。
                event_type="DATA_SOURCE_DEGRADED" if external else "CYCLE_FAILURE",
                status=RiskStatus.REJECTED,
                reason=str(error),
                metadata_json={"symbol": symbol, "external": external},
            )
        )
        self.db.commit()
        if external:
            # 只留痕，不动账户状态：对端恢复后下一轮自动继续。
            logger.warning("external data source failure, not counting toward the breaker: %s", error)
            return
        if self._consecutive_failures(user_id) >= self.settings.max_consecutive_failures:
            self._halt(
                user_id,
                RiskDecision(
                    status=RiskStatus.PAUSED, reasons=["repeated_cycle_failures"], halt=True
                ),
            )

    def _consecutive_failures(self, user_id: str) -> int:
        """自最近一次成功落库的决策以来，连续失败的周期数。

        没有成功决策做基准时，统计全部失败记录。
        """

        if self.db is None:
            return 0
        last_success = self.db.scalar(
            select(func.max(TradingDecision.created_at)).where(TradingDecision.user_id == user_id)
        )
        query = (
            select(func.count())
            .select_from(RiskEvent)
            .where(
                RiskEvent.user_id == user_id,
                RiskEvent.event_type == "CYCLE_FAILURE",
            )
        )
        if last_success is not None:
            query = query.where(RiskEvent.created_at > last_success)
        return self.db.scalar(query) or 0

    def market(self, user_id: str) -> dict[str, Any]:
        del user_id
        # 市场页的「数据时效规则」必须跟配置走：周期与新鲜度门都随
        # MARKET_TIMEFRAMES / MARKET_DATA_MAX_AGE_SECONDS 变，硬编码在页面里
        # 会立刻过时（页面曾写着 5m/1h/4h，而配置早已换成 12h/1d）。
        meta = {
            "timeframes": list(self.settings.timeframe_list),
            "max_age_seconds": self.settings.market_data_max_age_seconds,
        }
        if self.db is not None:
            rows = self.db.scalars(
                select(MarketSnapshotModel).order_by(MarketSnapshotModel.captured_at.desc()).limit(50)
            ).all()
            return {"items": [self._market_dict(row) for row in rows], **meta}
        return {"items": list(reversed(self._memory_market[-50:])), **meta}

    def decisions(
        self,
        user_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        symbol: str | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        """决策历史。可按品种 / 动作过滤。

        `total` 必须是**过滤后**的数量 —— 否则前端按它算页数会多出空白页。
        `symbols` 回传该用户实际出现过的品种，供前端做数据驱动的筛选项。
        """

        if self.db is not None:
            filters = [TradingDecision.user_id == user_id]
            if symbol:
                filters.append(TradingDecision.symbol == symbol)
            if action:
                filters.append(TradingDecision.action == action)
            total = self.db.scalar(
                select(func.count()).select_from(TradingDecision).where(*filters)
            ) or 0
            rows = self.db.scalars(
                select(TradingDecision)
                .where(*filters)
                .order_by(TradingDecision.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            available = self.db.scalars(
                select(TradingDecision.symbol)
                .where(TradingDecision.user_id == user_id)
                .distinct()
                .order_by(TradingDecision.symbol)
            ).all()
            leverage = self._effective_leverage(user_id)
            return {
                "items": [self._decision_dict(row, leverage=leverage) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "symbols": list(available),
            }
        items = [result.as_dict() for result in reversed(self._memory_results.get(user_id, []))]
        if symbol:
            items = [item for item in items if item.get("symbol") == symbol]
        if action:
            items = [item for item in items if item.get("action") == action]
        return {
            "items": items[(page - 1) * page_size : page * page_size],
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "symbols": sorted({str(item.get("symbol")) for item in items}),
        }

    def _effective_leverage(self, user_id: str) -> int | None:
        """账户实际生效的杠杆上限。

        虚拟盘固定杠杆、系统不下发提案杠杆，所以决策展示必须用账户值而不是
        提案占位值（HOLD 提案恒为 1，会让用户误以为系统用 1x 交易）。
        """

        if self.db is None:
            return None
        account = self._account_for_user(user_id)
        limits = self.risk_engine.limits.tightened(account.risk_limits if account else None)
        return limits.max_leverage

    def portfolio(self, user_id: str) -> dict[str, Any]:
        """当前持仓 —— **只含 OPEN**。

        positions 表是「当前状态」表（同一 symbol 复用一行），已平仓的行留在里面
        只为审计。把它们一起返回会让调用方把「已平仓」当成「持有中」：持仓表多出
        一行已平的仓位，概览的持仓数也会算错。审计走决策历史。
        """

        if self.db is not None:
            rows = self.db.scalars(
                select(Position).where(
                    Position.user_id == user_id,
                    Position.status == "OPEN",
                )
            ).all()
            return {"items": [self._position_dict(row) for row in rows]}
        return {"items": []}

    def events(self, user_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        if self.db is not None:
            total = self.db.scalar(
                select(func.count())
                .select_from(RiskEvent)
                .where(RiskEvent.user_id == user_id)
            ) or 0
            rows = self.db.scalars(
                select(RiskEvent)
                .where(RiskEvent.user_id == user_id)
                .order_by(RiskEvent.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [
                    {
                        "id": row.id,
                        "event_type": row.event_type,
                        "status": row.status.value if hasattr(row.status, "value") else row.status,
                        "reason": row.reason,
                        "created_at": row.created_at,
                    }
                    for row in rows
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    def close_position(self, user_id: str, symbol: str) -> dict[str, Any]:
        exchange = self._exchange_for_user(user_id)
        positions = [position for position in exchange.get_positions() if position.symbol == symbol]
        if not positions:
            return {"status": "NO_POSITION", "symbol": symbol}
        position = positions[0]
        proposal = TradeProposal(
            proposal_id=f"manual-close-{uuid4()}",
            action=Action.CLOSE,
            symbol=symbol,
            side="SELL" if position.side == "LONG" else "BUY",
            position_size_pct=0,
            leverage=1,
            valid_until=int(datetime.now(UTC).timestamp() * 1000),
            confidence=1,
            reasoning_summary="manual close",
            model_version="manual",
            trace_id=str(uuid4()),
        )
        result = self.execution_service.execute(
            exchange,
            proposal,
            RiskDecision(status=RiskStatus.ALLOWED, reasons=["manual_reduce_only"]),
            quantity=position.quantity,
        )
        return result.model_dump()

    @staticmethod
    def _market_dict(row: MarketSnapshotModel) -> dict[str, Any]:
        return {
            "symbol": row.symbol,
            "captured_at": row.captured_at,
            "last_price": float(row.last_price),
            "bid": float(row.bid) if row.bid is not None else None,
            "ask": float(row.ask) if row.ask is not None else None,
            "funding_rate": row.funding_rate,
            "open_interest": row.open_interest,
            "volume_24h": row.volume_24h,
        }

    @staticmethod
    def _decision_dict(row: TradingDecision, leverage: int | None = None) -> dict[str, Any]:
        return {
            "id": row.id,
            "cycle_id": row.cycle_id,
            "symbol": row.symbol,
            "action": row.action,
            "status": row.status,
            "proposal": row.proposal,
            "analyses": row.analyses,
            "risk_decision": row.risk_decision,
            "execution_result": row.execution_result,
            # 这次决策用的模型（committee 等）。智囊团 banner 的「模型路由」读它，
            # 而不是把模型名写死在词条里 —— 换模型时历史决策也要显示当时的模型。
            "model_versions": row.model_versions,
            "leverage": leverage,
            "created_at": row.created_at,
        }

    @staticmethod
    def _position_dict(row: Position) -> dict[str, Any]:
        return {
            "id": row.id,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": float(row.quantity),
            "entry_price": float(row.entry_price),
            "mark_price": float(row.mark_price) if row.mark_price is not None else None,
            "unrealized_pnl": float(row.unrealized_pnl),
            "status": row.status,
        }

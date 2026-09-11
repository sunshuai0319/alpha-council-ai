import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph import TradingCycleGraph, build_trading_cycle_graph
from app.agents.llm import ArkChatClient
from app.analytics.indicators import INDICATOR_VERSION, calculate_indicators
from app.collectors.weex import WeexCollector
from app.config import Settings, get_settings
from app.db.models import (
    AccountSnapshot,
    ControlState,
    MarketCandle,
    Position,
    RiskEvent,
    TradingAccount,
    TradingDecision,
)
from app.db.models import MarketSnapshot as MarketSnapshotModel
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import (
    ExecutionResult,
    RiskDecision,
    TradeProposal,
    TradingCycleState,
)
from app.exchange.base import ExchangeBalance, ExchangeClient, ExchangePosition
from app.exchange.weex import WeexClient, WeexCredentials
from app.execution.service import ExecutionService, stable_client_order_id
from app.rag.embeddings import BGEEmbedder, BGEReranker
from app.rag.milvus import MilvusVectorStore
from app.rag.retriever import Retriever
from app.reconciliation.service import ReconciliationService
from app.risk.engine import RiskEngine, daily_loss_pct

logger = logging.getLogger(__name__)


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
        candle_result, snapshot_result = collector.collect(
            symbols=(symbol,),
            timeframes=("5m", "1h", "4h"),
            limit=100,
        )
        snapshot = snapshot_result.items[0] if snapshot_result.items else None
        logger.info(
            "cycle market: user=%s symbol=%s price=%s candles=%d errors=%d",
            user_id,
            symbol,
            snapshot.last_price if snapshot else None,
            len(candle_result.items),
            len(candle_result.errors) + len(snapshot_result.errors),
        )
        state = TradingCycleState(
            user_id=user_id,
            cycle_id=cycle_id,
            started_at=started_at,
            symbol=symbol,
            market_snapshot=snapshot,
            candles_by_timeframe={
                timeframe: [candle for candle in candle_result.items if candle.timeframe == timeframe]
                for timeframe in ("5m", "1h", "4h")
            },
            technical_indicators=calculate_indicators(
                {
                    timeframe: [candle for candle in candle_result.items if candle.timeframe == timeframe]
                    for timeframe in ("5m", "1h", "4h")
                }
            ),
            errors=[*candle_result.errors, *snapshot_result.errors],
        )
        state = state.model_copy(update={"data_versions": {"technical_indicators": INDICATOR_VERSION}})
        self._persist_market_data(candle_result.items, snapshot_result.items)
        halt_reason = (
            None
            if self.control_status(user_id) != "PAUSED"
            else "paused"
        )
        if halt_reason is None and not self.settings.trading_enabled:
            halt_reason = "trading_disabled"
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
            graph = (
                build_trading_cycle_graph(llm=llm, settings=self.settings)
                if llm is not None
                else self.graph_factory()
            )
            logger.info("cycle committee: user=%s symbol=%s (running agents + LLM)", user_id, symbol)
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
            account = self._account_for_user(user_id)
            if account is not None:
                self.reconciliation.reconcile(
                    exchange,
                    user_id=user_id,
                    trading_account_id=account.id,
                    order_ids=[execution.exchange_order_id] if execution and execution.exchange_order_id else [],
                    symbol=symbol,
                )
        persisted = self._persist(result_state=state, risk=risk_decision, execution=execution)
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
        if snapshot:
            self._memory_market.append(snapshot.model_dump())
        return result

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
        data_age = (
            (now_ms - state.market_snapshot.captured_at) / 1000 if state.market_snapshot else float("inf")
        )
        if proposal.action is Action.HOLD:
            return RiskDecision(status=RiskStatus.ALLOWED, reasons=["hold_no_order"], checked_at=now_ms)
        try:
            balance = self._balance_for(exchange)
            positions = exchange.get_positions()
        except Exception as exc:  # noqa: BLE001 - exchange outage fails closed
            return RiskDecision(status=RiskStatus.REJECTED, reasons=[f"account_unavailable:{exc}"], checked_at=now_ms)
        if balance is None or state.market_snapshot is None:
            return RiskDecision(status=RiskStatus.REJECTED, reasons=["account_or_market_missing"], checked_at=now_ms)
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
            },
            risk_decision=risk.model_dump(mode="json"),
            # JSON 列：必须用 mode="json"，否则 Decimal（成交均价/已实现盈亏）存不进去。
            execution_result=execution.model_dump(mode="json") if execution else None,
            data_versions=result_state.data_versions,
            model_versions=result_state.model_versions,
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

    def _persist_market_data(self, candles: list[Any], snapshots: list[Any]) -> None:
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
        for snapshot in snapshots:
            self.db.add(
                MarketSnapshotModel(
                    symbol=snapshot.symbol,
                    captured_at=datetime.fromtimestamp(snapshot.captured_at / 1000, tz=UTC),
                    last_price=snapshot.last_price,
                    bid=snapshot.bid,
                    ask=snapshot.ask,
                    funding_rate=snapshot.funding_rate,
                    open_interest=snapshot.open_interest,
                    volume_24h=snapshot.volume_24h,
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
        self.db.add(
            RiskEvent(
                id=str(uuid4()),
                user_id=user_id,
                event_type="CYCLE_FAILURE",
                status=RiskStatus.REJECTED,
                reason=str(error),
                metadata_json={"symbol": symbol},
            )
        )
        self.db.commit()
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
        if self.db is not None:
            rows = self.db.scalars(
                select(MarketSnapshotModel).order_by(MarketSnapshotModel.captured_at.desc()).limit(50)
            ).all()
            return {"items": [self._market_dict(row) for row in rows]}
        return {"items": list(reversed(self._memory_market[-50:]))}

    def decisions(self, user_id: str) -> dict[str, Any]:
        if self.db is not None:
            rows = self.db.scalars(
                select(TradingDecision)
                .where(TradingDecision.user_id == user_id)
                .order_by(TradingDecision.created_at.desc())
                .limit(100)
            ).all()
            leverage = self._effective_leverage(user_id)
            return {"items": [self._decision_dict(row, leverage=leverage) for row in rows]}
        return {"items": [result.as_dict() for result in reversed(self._memory_results.get(user_id, []))]}

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
        if self.db is not None:
            rows = self.db.scalars(select(Position).where(Position.user_id == user_id)).all()
            return {"items": [self._position_dict(row) for row in rows]}
        return {"items": []}

    def events(self, user_id: str) -> dict[str, Any]:
        if self.db is not None:
            rows = self.db.scalars(
                select(RiskEvent)
                .where(RiskEvent.user_id == user_id)
                .order_by(RiskEvent.created_at.desc())
                .limit(100)
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
                ]
            }
        return {"items": []}

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

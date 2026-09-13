from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import OrderStatus, RiskStatus


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    clerk_user_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    #: 界面语言（zh-CN / en-US）：worker 按它决定 LLM 分析文本的语言。
    locale: Mapped[str] = mapped_column(String(16), default="zh-CN", nullable=False, server_default="zh-CN")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClerkWebhookEvent(Base):
    __tablename__ = "clerk_webhook_events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class WalletChallenge(Base):
    __tablename__ = "wallet_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    address: Mapped[str] = mapped_column(String(128))
    chain: Mapped[str] = mapped_column(String(32))
    nonce: Mapped[str] = mapped_column(String(128), unique=True)
    message: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ControlState(Base):
    __tablename__ = "control_states"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    #: 用户刚点「恢复周期」的标记：scheduler 发现 RUNNING + pending 就立即跑一轮，
    #: 不必等满 5 分钟。resume 置位、pause 清除、scheduler 消费后复位。
    pending_immediate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class WalletAddress(TimestampMixin, Base):
    __tablename__ = "wallet_addresses"
    __table_args__ = (UniqueConstraint("user_id", "address", "chain", name="uq_wallet_user_address"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    clerk_wallet_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str] = mapped_column(String(128))
    chain: Mapped[str] = mapped_column(String(32))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")


class TradingAccount(TimestampMixin, Base):
    __tablename__ = "trading_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="weex")
    environment: Mapped[str] = mapped_column(String(32), default="virtual")
    api_key_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    api_secret_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    passphrase_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 账户级风控偏好，只允许比平台上限更严（见 RiskLimits.tightened）。
    #: NULL 表示完全使用平台默认。
    risk_limits: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_market_candle_identity"),
        Index("ix_market_candles_lookup", "symbol", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    open_time: Mapped[int] = mapped_column(BigInteger)
    open: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    high: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    low: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    close: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    volume: Mapped[Decimal] = mapped_column(Numeric(40, 12))
    source: Mapped[str] = mapped_column(String(32), default="weex")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshots_lookup", "symbol", "captured_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_price: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    open_24h: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    high_24h: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    low_24h: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    price_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    index_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    orderbook_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MarketMicrostructureRecord(Base):
    """盘口 / 订单流 / 衍生品的观测。

    与 market_snapshots 分表：这类数据采集失败的模式与 ticker 不同（ticker 成功而
    depth 失败是常态），合表就分不清「没采到」与「采到但是空」。
    虚拟盘上这些数值疑似合成，目前只记录、不参与决策。
    """

    __tablename__ = "market_microstructures"
    __table_args__ = (Index("ix_market_microstructures_lookup", "symbol", "captured_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_imbalance: Mapped[float | None] = mapped_column(Float, nullable=True)
    taker_buy_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    available_margin: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    equity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    margin_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)


class Position(TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("user_id", "trading_account_id", "symbol", name="uq_position_scope"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    #: 开仓时的初始止损。它定义 1R 的距离（R = |entry_price - stop_loss|），
    #: 保本与移动止损都以它为基准。
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    #: 软件层当前生效的止损。移动止损只上移不下移；触及即下 reduceOnly 平仓。
    #: 交易所侧那条宽灾难止损不随它变（改不了也撤不掉）。
    effective_stop: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    #: 开仓时刻，时间止损用。
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 持仓期内最有利的价格（多仓取最高、空仓取最低），移动止损用它推算。
    peak_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    #: 首次达到近目标阈值的时刻，超时仍未达到 TP 时释放仓位。
    near_target_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("user_id", "client_order_id", name="uq_order_client_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("trading_decisions.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16), default="MARKET")
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    client_order_id: Mapped[str] = mapped_column(String(128))
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=OrderStatus.PENDING.value)
    request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("user_id", "exchange_fill_id", name="uq_fill_exchange_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    exchange_fill_id: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    price: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    fee: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CollectorError(Base):
    """采集错误，按 (collector, message) 归并计数。

    只写日志的话，"网络到底稳不稳"这类问题无从回答 —— 翻日志数不出错误率，
    也看不出是单个站点的波动还是整条出口的故障。
    """

    __tablename__ = "collector_errors"
    __table_args__ = (UniqueConstraint("collector", "message", name="uq_collector_error"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    collector: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TradingDecision(TimestampMixin, Base):
    __tablename__ = "trading_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str | None] = mapped_column(ForeignKey("trading_accounts.id"), nullable=True)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analyses: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    data_versions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_versions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 规则信号器的 composite 分数（spec 4.2 前向验证：哪些分项真与收益相关）。
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: LLM 否决结局：veto_none / veto_applied / veto_fail_closed。
    veto_type: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PnlSnapshot(Base):
    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trading_account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("trading_decisions.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[RiskStatus] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class SourceDocument(TimestampMixin, Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_source_document_url"),
        UniqueConstraint("content_hash", name="uq_source_document_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    canonical_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleaned_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(128), index=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    processing_status: Mapped[str] = mapped_column(String(32), default="NEW")
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0)


class DocumentSummary(TimestampMixin, Base):
    __tablename__ = "document_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), unique=True)
    summary: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(64))
    assets: Mapped[list] = mapped_column(JSON, default=list)
    direction: Mapped[str] = mapped_column(String(16))
    impact_horizon: Mapped[str] = mapped_column(String(128))
    confidence: Mapped[float] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(128))
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MacroObservationRecord(Base):
    __tablename__ = "macro_observations"
    __table_args__ = (
        UniqueConstraint("series_id", "observation_date", name="uq_macro_observation"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String(64), index=True)
    observation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

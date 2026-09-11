import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import Action, RiskStatus


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = "weex"

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.symbol, self.timeframe, self.open_time


class MarketSnapshot(BaseModel):
    symbol: str
    captured_at: int
    last_price: float
    #: 24h 区间与量能。WEEX 的 ticker 全部返回，早期实现丢掉了。
    open_24h: float | None = None
    high_24h: float | None = None
    low_24h: float | None = None
    price_change_pct: float | None = None
    quote_volume_24h: float | None = None
    #: 标记价与指数价，用来算基差。
    mark_price: float | None = None
    index_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    volume_24h: float | None = None
    source: str = "weex"


def _as_str_list(value: Any) -> Any:
    """LLM 常把列表字段写成单个字符串（中文时尤其容易合并成一段话），收敛成列表。"""

    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return value


class AnalysisResult(BaseModel):
    status: str = "PROPOSED"
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    model_version: str
    trace_id: str

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _coerce_evidence_refs(cls, value: Any) -> Any:
        return _as_str_list(value)


class TradeProposal(AnalysisResult):
    proposal_id: str
    action: Action
    symbol: str
    side: str | None = None
    position_size_pct: float = Field(ge=0, le=1)
    leverage: int = Field(default=1, ge=0)
    stop_loss: float | None = None
    take_profit: float | None = None
    valid_until: int | None = None
    invalidation_conditions: list[str] = Field(default_factory=list)

    @field_validator("side")
    @classmethod
    def normalize_side(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @field_validator("invalidation_conditions", mode="before")
    @classmethod
    def _coerce_invalidation_conditions(cls, value: Any) -> Any:
        return _as_str_list(value)

    @field_validator("valid_until", mode="before")
    @classmethod
    def parse_valid_until(cls, value: Any) -> int | None:
        """LLM 常把有效期输出成 ISO 字符串（甚至是编造的过去日期），收敛成毫秒。

        null / 空串 / 无法解析 → None，交给 enforce_signal_fields 兜底
        （HOLD 填当前时间、非 HOLD 拒绝）。
        """

        if value is None or value == "" or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
            try:
                return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                return None
        return None

    @model_validator(mode="after")
    def enforce_signal_fields(self) -> "TradeProposal":
        """HOLD 不下单，放行 LLM 的 leverage=0 / valid_until=null 并归一化。

        实测 LLM 对 HOLD 常返回"无操作"值（leverage=0、valid_until 为 null 或
        ISO 字符串），严格 schema 会把整个提案拒掉、fallback 成 safe-hold，
        真实的观望判断永远到不了风控。非 HOLD（真实信号）仍然强制杠杆 >= 1
        且有效期必填。
        """

        if self.action is Action.HOLD:
            if self.leverage < 1:
                self.leverage = 1
            # HOLD 不下单，valid_until 无实际意义：不信任 LLM 的 null / ISO / 编造日期。
            self.valid_until = int(time.time() * 1000)
            return self
        if self.leverage < 1:
            raise ValueError("non-HOLD proposal requires leverage >= 1")
        if self.valid_until is None:
            raise ValueError("non-HOLD proposal requires valid_until")
        return self


class RiskDecision(BaseModel):
    status: RiskStatus
    reasons: list[str] = Field(default_factory=list)
    adjusted_position_size_pct: float | None = Field(default=None, ge=0, le=1)
    checked_at: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: 账户级熔断（日亏损、连亏、权益非正）。True 表示应当暂停该账户，
    #: 而不只是拒掉当前这一单。
    halt: bool = False

    @property
    def allowed(self) -> bool:
        return self.status is RiskStatus.ALLOWED


class ExecutionResult(BaseModel):
    status: str
    proposal_id: str
    client_order_id: str
    exchange_order_id: str | None = None
    message: str | None = None
    #: 成交均价；下单响应本身没有，由回查得到，拿不到时为 None。
    average_price: Decimal | None = None
    #: 平仓回合的价差盈亏（**不含手续费**），用于连亏熔断；非平仓时为 None。
    realized_pnl: Decimal | None = None


class TradingCycleState(BaseModel):
    tenant_id: str | None = None
    user_id: str
    cycle_id: str
    started_at: int
    symbol: str
    #: 用户界面语言：LLM 生成的分析文本按它选择语言（见 graph._language_instruction）。
    locale: str = "zh-CN"
    market_snapshot: MarketSnapshot | None = None
    candles_by_timeframe: dict[str, list[Candle]] = Field(default_factory=dict)
    technical_indicators: dict[str, Any] = Field(default_factory=dict)
    news_items: list[dict[str, Any]] = Field(default_factory=list)
    macro_events: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_evidence: list[dict[str, Any]] = Field(default_factory=list)
    market_analysis: AnalysisResult | None = None
    quant_analysis: AnalysisResult | None = None
    macro_analysis: AnalysisResult | None = None
    risk_assessment: RiskDecision | None = None
    trade_proposal: TradeProposal | None = None
    execution_result: ExecutionResult | None = None
    errors: list[str] = Field(default_factory=list)
    data_versions: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    trace_ids: list[str] = Field(default_factory=list)

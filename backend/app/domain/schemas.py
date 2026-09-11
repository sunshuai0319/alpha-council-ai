import time
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
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


class MarketMicrostructure(BaseModel):
    """一次观测的盘口 / 订单流 / 衍生品快照。

    与 ``MarketSnapshot`` 分开建模，因为失败模式不同 —— ticker 成功而 depth 失败
    是常态，合在一起就分不清「没采到」与「采到但是空」。

    ⚠️ 虚拟盘的这些数值疑似合成数据（实测价差低到 0.00013%），只可作辅助确认项。
    """

    symbol: str
    captured_at: int
    bid: float | None = None
    ask: float | None = None
    spread_bps: float | None = None
    #: (买量 - 卖量) / (买量 + 卖量)，取前 5 档。
    depth_imbalance: float | None = None
    #: 主动买量 / 总成交量。
    taker_buy_ratio: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
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
    #: 默认值而不是必填：LLM 漏写这两个字段时，分析不该整条被 schema 拒掉
    #: （19 条决策里有 4 条 quant 因此落到 deterministic-fallback）。
    model_version: str = "deterministic-fallback"
    trace_id: str = ""

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
                # fromisoformat 自 3.11 起直接接受尾部的 "Z"，不必再手工替换。
                return int(datetime.fromisoformat(text).timestamp() * 1000)
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
            self.leverage = max(self.leverage, 1)
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
    #: 盘口/订单流/衍生品观测。structure veto agent 靠它判断流动性异常 ——
    #: 这是它与 news veto agent 数据域不同的关键。
    microstructure: MarketMicrostructure | None = None
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
    #: 规则信号器的 composite 分数（spec 4.2 前向验证用）。
    signal_score: float | None = None
    #: LLM 否决结局：veto_none / veto_applied / veto_invalid_ignored。
    veto_type: str | None = None
    #: 每个专业 veto agent 的独立结论，按 agent 名索引。
    #: 记录它才能算「每个 agent 的否决精度」—— 拦掉的单子里多少事后看是对的。
    veto_verdicts: dict[str, Any] = Field(default_factory=dict)
    #: 当前权益，cycle 注入给 signal_node 做仓位反推。
    equity: Decimal | None = None
    #: signal_node 决策的时刻（ms）。风控的 data_age 以它为基准，而不是采集时刻 ——
    #: 否则 LLM 否决耗时（最坏 = 重试 3 次 × 60s 超时）会被误算成行情过期。
    signal_decided_at: int | None = None
    errors: list[str] = Field(default_factory=list)
    data_versions: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    trace_ids: list[str] = Field(default_factory=list)


class VetoReason(StrEnum):
    """LLM 否决的合法理由 —— 封闭枚举。

    `证据不足` 不是其中之一：证据不足时规则信号器自己就 HOLD 了，LLM 再用这条
    否决就是在重复信号器已经做过的事，只会让单子永远开不出来（spec 2.3）。
    """

    REGIME_CONFLICT = "REGIME_CONFLICT"
    NEWS_SHOCK = "NEWS_SHOCK"
    STRUCTURE_INVALIDATED = "STRUCTURE_INVALIDATED"
    LIQUIDITY_ANOMALY = "LIQUIDITY_ANOMALY"
    DATA_INTEGRITY = "DATA_INTEGRITY"


class VetoVerdict(BaseModel):
    """LLM 否决节点的输出。

    三种结局（spec 2.3）：
    - veto=False → 放行（veto_none）
    - veto=True 且有证据 → 拦截（veto_applied）
    - veto=True 但证据为空 / 理由不在枚举内 → 放行 + 计数（veto_invalid_ignored）
      这个校验由 schema 承担：veto=True 无证据直接 ValidationError。
    """

    veto: bool
    reasons: list[VetoReason] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""

    @model_validator(mode="after")
    def veto_requires_evidence(self) -> "VetoVerdict":
        if self.veto and not self.evidence_refs:
            raise ValueError("veto=True requires evidence_refs")
        return self

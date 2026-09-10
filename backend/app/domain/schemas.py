from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    bid: float | None = None
    ask: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    volume_24h: float | None = None
    source: str = "weex"


class AnalysisResult(BaseModel):
    status: str = "PROPOSED"
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    model_version: str
    trace_id: str


class TradeProposal(AnalysisResult):
    proposal_id: str
    action: Action
    symbol: str
    side: str | None = None
    position_size_pct: float = Field(ge=0, le=1)
    leverage: int = Field(ge=1)
    stop_loss: float | None = None
    take_profit: float | None = None
    valid_until: int
    invalidation_conditions: list[str] = Field(default_factory=list)

    @field_validator("side")
    @classmethod
    def normalize_side(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None


class RiskDecision(BaseModel):
    status: RiskStatus
    reasons: list[str] = Field(default_factory=list)
    adjusted_position_size_pct: float | None = Field(default=None, ge=0, le=1)
    checked_at: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.status is RiskStatus.ALLOWED


class ExecutionResult(BaseModel):
    status: str
    proposal_id: str
    client_order_id: str
    exchange_order_id: str | None = None
    message: str | None = None


class TradingCycleState(BaseModel):
    tenant_id: str | None = None
    user_id: str
    cycle_id: str
    started_at: int
    symbol: str
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

import pytest
from pydantic import ValidationError

from app.db.models import Base
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import AnalysisResult, Candle, RiskDecision, TradeProposal


def test_candle_identity_is_symbol_timeframe_open_time():
    candle = Candle(
        symbol="BTC-USDT",
        timeframe="5m",
        open_time=1700000000,
        open=1,
        high=2,
        low=0.5,
        close=1.5,
        volume=10,
    )
    assert candle.identity == ("BTC-USDT", "5m", 1700000000)


def test_trade_proposal_uses_explicit_action_and_safety_fields():
    proposal = TradeProposal(
        proposal_id="p-1",
        action=Action.LONG,
        symbol="BTC-USDT",
        side="BUY",
        position_size_pct=0.1,
        leverage=2,
        stop_loss=90,
        take_profit=120,
        valid_until=1700000300,
        invalidation_conditions=["close_below_90"],
        confidence=0.72,
        reasoning_summary="Trend alignment",
        evidence_refs=["doc-1"],
        model_version="test-model",
        trace_id="trace-1",
    )
    assert proposal.action is Action.LONG
    assert proposal.leverage == 2


def test_hold_proposal_accepts_committee_style_zero_leverage_and_null_valid_until():
    """LLM 对 HOLD 常返回 leverage=0 / valid_until=null（它认为不下单）。

    之前这两个值违反 schema 导致整个提案被拒、fallback 成 safe-hold，真实的
    HOLD 判断永远进不了风控。HOLD 必须放行并归一化。
    """
    proposal = TradeProposal.model_validate(
        {
            "proposal_id": "p-hold",
            "action": Action.HOLD,
            "symbol": "BTC-USDT",
            "position_size_pct": 0,
            "leverage": 0,
            "valid_until": None,
            "confidence": 0.0,
            "reasoning_summary": "Signals mixed; wait.",
            "evidence_refs": [],
            "model_version": "committee-v1",
            "trace_id": "t-1",
        }
    )
    assert proposal.action is Action.HOLD
    assert proposal.leverage >= 1
    assert proposal.valid_until is not None


def test_proposal_accepts_scalar_invalidation_conditions():
    """LLM 常把失效条件写成一段字符串而不是数组（中文尤其容易合并成一段话）。"""
    proposal = TradeProposal.model_validate(
        {
            "proposal_id": "p-1",
            "action": Action.HOLD,
            "symbol": "BTC-USDT",
            "position_size_pct": 0,
            "leverage": 1,
            "valid_until": 1700000300,
            "invalidation_conditions": "证据缺失或不足；等待一致方向",
            "confidence": 0.3,
            "reasoning_summary": "观望",
            "evidence_refs": "doc-1",
            "model_version": "committee-agent-v1",
            "trace_id": "t",
        }
    )
    assert proposal.invalidation_conditions == ["证据缺失或不足；等待一致方向"]
    assert proposal.evidence_refs == ["doc-1"]


def test_analysis_accepts_scalar_evidence_refs():
    """evidence_refs 同样可能被写成字符串。"""
    result = AnalysisResult.model_validate(
        {
            "status": "neutral",
            "confidence": 0.4,
            "reasoning_summary": "中性",
            "evidence_refs": "doc-1",
            "model_version": "m",
            "trace_id": "t",
        }
    )
    assert result.evidence_refs == ["doc-1"]


def test_hold_proposal_accepts_iso_or_garbage_valid_until():
    """LLM 对 valid_until 的输出格式漂移：ISO 字符串、编造日期、乱串都出现过。"""
    base = {
        "proposal_id": "p-hold",
        "action": Action.HOLD,
        "symbol": "BTC-USDT",
        "position_size_pct": 0,
        "confidence": 0.0,
        "reasoning_summary": "wait",
        "evidence_refs": [],
        "model_version": "committee-v1",
        "trace_id": "t-3",
    }
    for bad_value in ["2025-04-11T00:00:00Z", "soon", 0]:
        proposal = TradeProposal.model_validate({**base, "leverage": 0, "valid_until": bad_value})
        assert proposal.valid_until is not None and isinstance(proposal.valid_until, int)


def test_non_hold_proposal_rejects_invalid_leverage_or_valid_until():
    """真实信号（LONG/SHORT/CLOSE）仍要求合法杠杆与有效期，防 LLM 乱填。"""
    base = {
        "proposal_id": "p-long",
        "action": Action.LONG,
        "symbol": "BTC-USDT",
        "side": "BUY",
        "position_size_pct": 0.1,
        "stop_loss": 90,
        "take_profit": 120,
        "invalidation_conditions": [],
        "confidence": 0.7,
        "reasoning_summary": "Breakout confirmed.",
        "evidence_refs": [],
        "model_version": "committee-v1",
        "trace_id": "t-2",
    }
    with pytest.raises(ValidationError):
        TradeProposal.model_validate({**base, "leverage": 0, "valid_until": 1700000300})
    with pytest.raises(ValidationError):
        TradeProposal.model_validate({**base, "leverage": 2, "valid_until": None})

    # ISO 字符串解析成毫秒时间戳后放行
    proposal = TradeProposal.model_validate({**base, "leverage": 2, "valid_until": "2026-09-12T00:00:00Z"})
    assert isinstance(proposal.valid_until, int)


def test_risk_decision_defaults_to_rejected():
    decision = RiskDecision(status=RiskStatus.REJECTED, reasons=["stale_data"])
    assert decision.allowed is False


def test_schema_contains_user_scoped_trading_tables():
    assert {
        "users",
        "trading_accounts",
        "orders",
        "positions",
        "trading_decisions",
        "source_documents",
    }.issubset(Base.metadata.tables)
    assert "uq_market_candle_identity" in {
        constraint.name
        for constraint in Base.metadata.tables["market_candles"].constraints
        if constraint.name
    }

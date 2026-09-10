from app.db.models import Base
from app.domain.enums import Action, RiskStatus
from app.domain.schemas import Candle, RiskDecision, TradeProposal


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

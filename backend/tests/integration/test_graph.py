from app.agents.graph import build_trading_cycle_graph
from app.config import Settings
from app.domain.schemas import MarketSnapshot, TradingCycleState


def test_compiled_langgraph_executes_safe_hold_without_external_tools() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )
    timestamp = 1_700_000_000_000
    state = TradingCycleState(
        user_id="user-1",
        cycle_id="integration-1",
        started_at=timestamp,
        symbol="ETH-USDT",
        market_snapshot=MarketSnapshot(
            symbol="ETH-USDT",
            captured_at=timestamp,
            last_price=2000,
        ),
    )
    graph = build_trading_cycle_graph(settings=settings, clock_ms=lambda: timestamp)
    result = graph.invoke(state)
    assert result.trade_proposal is not None
    assert result.trade_proposal.action == "HOLD"
    assert result.model_versions["committee"] == "deepseek-v4-pro-ga-260813"

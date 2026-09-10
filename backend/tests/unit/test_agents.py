from typing import Any

from app.agents.graph import build_trading_cycle_graph, run_committee
from app.config import Settings
from app.domain.enums import Action
from app.domain.schemas import MarketSnapshot, TradingCycleState


class FakeLLM:
    def __init__(self, response: str | dict[str, Any]) -> None:
        self.response = response

    def complete_json(self, prompt: str) -> str | dict[str, Any]:
        del prompt
        return self.response


def _state(captured_at: int = 1_700_000_000_000) -> TradingCycleState:
    return TradingCycleState(
        user_id="user-1",
        cycle_id="cycle-1",
        started_at=captured_at,
        symbol="BTC-USDT",
        market_snapshot=MarketSnapshot(
            symbol="BTC-USDT",
            captured_at=captured_at,
            last_price=100,
        ),
    )


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        MARKET_DATA_MAX_AGE_SECONDS=90,
    )


def test_invalid_committee_json_becomes_hold() -> None:
    result = run_committee(_state(), FakeLLM("not-json"))
    assert result.action == Action.HOLD
    assert result.position_size_pct == 0


def test_graph_contains_parallel_analysis_nodes() -> None:
    graph = build_trading_cycle_graph(settings=_settings(), clock_ms=lambda: 1_700_000_000_000)
    assert {"market_node", "quant_node", "macro_node"}.issubset(graph.node_names)


def test_graph_routes_stale_market_data_to_safe_hold() -> None:
    graph = build_trading_cycle_graph(settings=_settings(), clock_ms=lambda: 1_700_000_100_000)
    result = graph.invoke(_state())
    assert result.trade_proposal is not None
    assert result.trade_proposal.action == Action.HOLD
    assert "market_snapshot_stale" in result.errors


def test_graph_accepts_structured_committee_proposal_after_parallel_analysis() -> None:
    class CommitteeLLM:
        def complete_json(self, prompt: str) -> dict[str, Any]:
            if "Committee Agent" in prompt:
                return {
                    "proposal_id": "proposal-1",
                    "action": "LONG",
                    "symbol": "BTC-USDT",
                    "side": "BUY",
                    "position_size_pct": 0.1,
                    "leverage": 2,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "valid_until": 1_700_000_100_000,
                    "invalidation_conditions": ["close below stop"],
                    "confidence": 0.8,
                    "reasoning_summary": "trend and event agree",
                    "evidence_refs": ["evidence-1"],
                    "model_version": "deepseek-v4-pro-ga-260813",
                    "trace_id": "trace-1",
                }
            return {
                "status": "PROPOSED",
                "confidence": 0.7,
                "reasoning_summary": "neutral test analysis",
                "evidence_refs": ["evidence-1"],
                "model_version": "deepseek-v4-pro-ga-260813",
                "trace_id": "trace-analysis",
            }

    class Retriever:
        def retrieve(self, query: str, *, asset: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
            return [{"id": "evidence-1", "content": query, "asset": asset, "limit": limit}]

    graph = build_trading_cycle_graph(
        settings=_settings(),
        llm=CommitteeLLM(),
        retriever=Retriever(),
        clock_ms=lambda: 1_700_000_000_000,
    )
    result = graph.invoke(_state())
    assert result.trade_proposal is not None
    assert result.trade_proposal.action == Action.LONG
    assert result.errors == []

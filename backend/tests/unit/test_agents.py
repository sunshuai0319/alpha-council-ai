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


class CapturingLLM:
    def __init__(self, response: str | dict[str, Any]) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete_json(self, prompt: str) -> str | dict[str, Any]:
        self.prompts.append(prompt)
        return self.response


_VALID_HOLD = (
    '{"proposal_id":"p","action":"HOLD","symbol":"BTC-USDT","position_size_pct":0,'
    '"confidence":0.1,"reasoning_summary":"x","evidence_refs":[],"model_version":"m","trace_id":"t"}'
)


def test_committee_prompt_asks_for_chinese_when_locale_is_zh() -> None:
    """中文界面的用户，LLM 分析文本要输出简体中文。"""
    llm = CapturingLLM(_VALID_HOLD)
    run_committee(_state().model_copy(update={"locale": "zh-CN"}), llm)
    assert "简体中文" in llm.prompts[0]


def test_committee_prompt_stays_english_for_en_locale() -> None:
    llm = CapturingLLM(_VALID_HOLD)
    run_committee(_state().model_copy(update={"locale": "en-US"}), llm)
    assert "简体中文" not in llm.prompts[0]


def test_invalid_committee_json_becomes_hold() -> None:
    result = run_committee(_state(), FakeLLM("not-json"))
    assert result.action == Action.HOLD
    assert result.position_size_pct == 0


def test_graph_contains_rule_signal_and_specialized_veto_nodes() -> None:
    """拓扑：确定性信号器定方向，三个专业 veto 并行判断，任一否决即拦。"""
    graph = build_trading_cycle_graph(settings=_settings(), clock_ms=lambda: 1_700_000_000_000)
    assert {
        "signal_node",
        "news_veto_node",
        "structure_veto_node",
        "data_integrity_node",
        "merge_veto",
        "proposal_validator",
    }.issubset(graph.node_names)


def test_graph_routes_stale_market_data_to_safe_hold() -> None:
    graph = build_trading_cycle_graph(settings=_settings(), clock_ms=lambda: 1_700_000_100_000)
    result = graph.invoke(_state())
    assert result.trade_proposal is not None
    assert result.trade_proposal.action == Action.HOLD
    assert "market_snapshot_stale" in result.errors


def _bullish_state() -> TradingCycleState:
    """对齐的多头技术结构，让规则信号器给出 LONG。"""
    tf = {
        "trend": "BULLISH",
        "rsi_14": 58.0,
        "volume_change_1": 0.1,
        "ema_spread_pct": 0.001,
        "atr_14": 2.0,
    }
    return _state().model_copy(update={
        "technical_indicators": {
            "1h": tf,
            "4h": {**tf, "rsi_14": 60.0},
            "5m": {**tf, "rsi_14": 62.0},
        },
    })


class AllowLLM:
    """否决节点：放行（veto=False）。"""

    def complete_json(self, prompt: str) -> dict[str, Any]:
        del prompt
        return {
            "veto": False,
            "reasons": [],
            "evidence_refs": [],
            "reasoning_summary": "no objection",
        }


class VetoLLM:
    """否决节点：按各自的章程给出**域内**否决理由。

    用同一个理由回给两个 agent 会越界（见 _run_veto_agent 的章程检查），
    那测的就不是真实行为了。
    """

    def complete_json(self, prompt: str) -> dict[str, Any]:
        reason = "STRUCTURE_INVALIDATED" if "structure_liquidity" in prompt else "NEWS_SHOCK"
        return {
            "veto": True,
            "reasons": [reason],
            "evidence_refs": ["evidence-1"],
            "reasoning_summary": "contradicts the signal",
        }


def _retriever():
    class Retriever:
        def retrieve(self, query: str, *, asset: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
            return [{"id": "evidence-1", "content": query, "asset": asset, "limit": limit}]

    return Retriever()


def test_rule_signal_flows_to_long_when_llm_does_not_veto() -> None:
    """规则信号器给 LONG → LLM 放行 → 最终 LONG，且 signal_score 落库。"""
    graph = build_trading_cycle_graph(
        settings=_settings(),
        llm=AllowLLM(),
        retriever=_retriever(),
        clock_ms=lambda: 1_700_000_000_000,
    )
    result = graph.invoke(_bullish_state())
    assert result.trade_proposal is not None
    assert result.trade_proposal.action == Action.LONG
    assert result.signal_score is not None and result.signal_score > 0
    assert result.veto_type == "veto_none"
    assert result.errors == []


def test_rule_signal_is_blocked_when_llm_vetoes() -> None:
    """规则信号器给 LONG → LLM 以 NEWS_SHOCK 否决 → 最终 HOLD。"""
    graph = build_trading_cycle_graph(
        settings=_settings(),
        llm=VetoLLM(),
        retriever=_retriever(),
        clock_ms=lambda: 1_700_000_000_000,
    )
    result = graph.invoke(_bullish_state())
    assert result.trade_proposal is not None
    assert result.trade_proposal.action == Action.HOLD
    assert result.veto_type == "veto_applied"


def test_rule_signal_holds_when_indicators_conflict() -> None:
    """指标冲突 → 规则信号器直接 HOLD，不调 LLM、不走 retrieve。

    HOLD 走「signal_node → persist」短路，veto_node 根本没跑，所以 veto_type
    保持 None —— 这正是零 LLM 调用那条路径。
    """
    conflicting = {
        "1h": {"trend": "BULLISH", "rsi_14": 55.0, "atr_14": 2.0},
        "4h": {"trend": "BEARISH", "rsi_14": 42.0, "atr_14": 2.0},
        "5m": {"trend": "NEUTRAL", "rsi_14": 50.0, "atr_14": 2.0},
    }
    state = _state().model_copy(update={"technical_indicators": conflicting})
    graph = build_trading_cycle_graph(settings=_settings(), clock_ms=lambda: 1_700_000_000_000)
    result = graph.invoke(state)
    assert result.trade_proposal is not None
    assert result.trade_proposal.action == Action.HOLD
    assert result.veto_type is None

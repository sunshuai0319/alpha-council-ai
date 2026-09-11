"""专业 veto agent：各自域独立判断，任一 veto 即拦截。

关键性质：
- 两个 LLM agent 拿到**不同的数据域**（新闻 vs 结构），不是同一批数据的复读
- 数据完整性是确定性检查，不调 LLM
- 合并是「任一否决即拦」，不是多数表决 —— veto 是安全机制
"""

from typing import Any

from app.agents.graph import (
    VetoReason,
    data_integrity_node,
    merge_veto_node,
    news_veto_node,
    structure_veto_node,
)
from app.domain.enums import Action
from app.domain.schemas import (
    MarketMicrostructure,
    MarketSnapshot,
    TradeProposal,
    TradingCycleState,
)


def _state(**overrides: Any) -> TradingCycleState:
    base = {
        "user_id": "u-1",
        "cycle_id": "c-1",
        "started_at": 1_700_000_000_000,
        "symbol": "BTC-USDT",
        "market_snapshot": MarketSnapshot(
            symbol="BTC-USDT",
            captured_at=1_700_000_000_000,
            last_price=100.0,
            high_24h=110.0,
            low_24h=90.0,
        ),
        "trade_proposal": TradeProposal(
            proposal_id="signal-c-1",
            action=Action.SHORT,
            symbol="BTC-USDT",
            side="SHORT",
            position_size_pct=0.2,
            leverage=1,
            stop_loss=103.0,
            take_profit=94.0,
            valid_until=1_700_000_300_000,
            confidence=0.6,
            reasoning_summary="rule signal SHORT score=-0.60",
            evidence_refs=["indicators.1h.trend"],
            model_version="rule-signal-v1",
            trace_id="t-1",
        ),
    }
    return TradingCycleState(**{**base, **overrides})


class RecordingLLM:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete_json(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        return self.response


PASS = {"veto": False, "reasons": [], "evidence_refs": [], "reasoning_summary": "ok"}
VETO_NEWS = {
    "veto": True,
    "reasons": ["NEWS_SHOCK"],
    "evidence_refs": ["doc-1"],
    "reasoning_summary": "exchange outage headline",
}
VETO_STRUCTURE = {
    "veto": True,
    "reasons": ["STRUCTURE_INVALIDATED"],
    "evidence_refs": ["indicators.1h.trend"],
    "reasoning_summary": "trend flipped",
}


def test_news_agent_only_sees_news_and_macro_data() -> None:
    """两个 agent 必须读不同的数据，否则「多 agent」只是同一模型的复读。"""
    llm = RecordingLLM(PASS)
    state = _state(
        retrieved_evidence=[{"id": "doc-1", "content": "headline"}],
        macro_events=[{"kind": "macro_series", "series_id": "DGS10"}],
        microstructure=MarketMicrostructure(symbol="BTC-USDT", captured_at=1, spread_bps=1.0),
    )

    news_veto_node(state, llm=llm)

    prompt = llm.prompts[0]
    assert "doc-1" in prompt
    assert "DGS10" in prompt
    assert "STRUCTURE" not in prompt.upper() or "another agent covers that" in prompt


def test_structure_agent_only_sees_technical_and_book_data() -> None:
    llm = RecordingLLM(PASS)
    state = _state(
        technical_indicators={"1h": {"trend": "BEARISH", "atr_14": 2.0}},
        microstructure=MarketMicrostructure(
            symbol="BTC-USDT", captured_at=1, spread_bps=1.0, depth_imbalance=-0.4
        ),
        retrieved_evidence=[{"id": "doc-1", "content": "headline"}],
    )

    structure_veto_node(state, llm=llm)

    prompt = llm.prompts[0]
    assert "BEARISH" in prompt
    assert "depth_imbalance" in prompt


def test_news_veto_is_recorded_with_its_own_key() -> None:
    result = news_veto_node(_state(), llm=RecordingLLM(VETO_NEWS))
    verdict = result["veto_verdicts"]["news_macro"]
    assert verdict["veto"] is True
    assert verdict["status"] == "applied"
    assert "NEWS_SHOCK" in verdict["reasons"]


def test_structure_veto_is_recorded_with_its_own_key() -> None:
    result = structure_veto_node(_state(), llm=RecordingLLM(VETO_STRUCTURE))
    verdict = result["veto_verdicts"]["structure_liquidity"]
    assert verdict["veto"] is True
    assert "STRUCTURE_INVALIDATED" in verdict["reasons"]


def test_agents_are_skipped_on_hold_proposals() -> None:
    """HOLD 提案不该调 LLM —— 那正是零调用路径。"""
    hold = _state().trade_proposal.model_copy(update={"action": Action.HOLD})
    llm = RecordingLLM(PASS)
    assert news_veto_node(_state(trade_proposal=hold), llm=llm) == {}
    assert structure_veto_node(_state(trade_proposal=hold), llm=llm) == {}
    assert llm.prompts == []


def test_agents_are_skipped_without_an_llm() -> None:
    """没配 LLM 时放行，不能因为缺 LLM 把单子变成 HOLD。"""
    result = news_veto_node(_state(), llm=None)
    assert result["veto_verdicts"]["news_macro"]["veto"] is False


def test_invalid_agent_output_is_ignored_not_blocking() -> None:
    """LLM 输出格式错 = 放行 + 计数，不该拦住一个已经过了规则的信号。"""
    result = news_veto_node(_state(), llm=RecordingLLM({"veto": True, "reasons": ["WHATEVER"]}))
    verdict = result["veto_verdicts"]["news_macro"]
    assert verdict["veto"] is False
    assert verdict["status"] == "invalid_ignored"


def test_data_integrity_flags_absurd_spread_without_calling_an_llm() -> None:
    state = _state(microstructure=MarketMicrostructure(symbol="BTC-USDT", captured_at=1, spread_bps=500.0))
    verdict = data_integrity_node(state)["veto_verdicts"]["data_integrity"]
    assert verdict["veto"] is True
    assert "DATA_INTEGRITY" in verdict["reasons"]


def test_data_integrity_flags_crossed_book() -> None:
    state = _state(
        microstructure=MarketMicrostructure(symbol="BTC-USDT", captured_at=1, bid=101.0, ask=100.0)
    )
    assert data_integrity_node(state)["veto_verdicts"]["data_integrity"]["veto"] is True


def test_data_integrity_passes_on_missing_microstructure() -> None:
    """数据缺失不是异常 —— 缺项不该整体 HOLD（spec 2.1）。"""
    assert data_integrity_node(_state())["veto_verdicts"]["data_integrity"]["veto"] is False


def test_vetoing_outside_your_charter_is_ignored() -> None:
    """news agent 用 STRUCTURE_INVALIDATED 否决 = 越界，记无效放行。

    接受它会让「按域独立」失去意义，也会让按 agent 统计的否决精度互相污染。
    """
    result = news_veto_node(_state(), llm=RecordingLLM(VETO_STRUCTURE))
    verdict = result["veto_verdicts"]["news_macro"]
    assert verdict["veto"] is False
    assert verdict["status"] == "invalid_ignored"
    assert verdict["out_of_charter"] == ["STRUCTURE_INVALIDATED"]


def test_merge_blocks_when_any_agent_vetoes() -> None:
    """任一否决即拦截：安全机制不能用多数表决。"""
    state = _state(
        veto_verdicts={
            "news_macro": {"veto": True, "status": "applied", "reasons": ["NEWS_SHOCK"]},
            "structure_liquidity": {"veto": False, "status": "none"},
            "data_integrity": {"veto": False, "status": "none"},
        }
    )
    result = merge_veto_node(state, now_ms=1_700_000_100_000)
    assert result["veto_type"] == "veto_applied"
    assert result["trade_proposal"]["action"] == "HOLD"
    assert "NEWS_SHOCK" in result["trade_proposal"]["reasoning_summary"]


def test_merge_passes_when_nobody_vetoes() -> None:
    state = _state(
        veto_verdicts={
            "news_macro": {"veto": False, "status": "none"},
            "structure_liquidity": {"veto": False, "status": "none"},
            "data_integrity": {"veto": False, "status": "none"},
        }
    )
    assert merge_veto_node(state)["veto_type"] == "veto_none"


def test_merge_reports_invalid_ignored_when_an_agent_malformed() -> None:
    state = _state(veto_verdicts={"news_macro": {"veto": False, "status": "invalid_ignored"}})
    assert merge_veto_node(state)["veto_type"] == "veto_invalid_ignored"


def test_veto_reason_enum_still_covers_every_charter() -> None:
    """每个 agent 的专属理由都必须落在封闭枚举里。"""
    for reason in ("NEWS_SHOCK", "REGIME_CONFLICT", "STRUCTURE_INVALIDATED", "LIQUIDITY_ANOMALY", "DATA_INTEGRITY"):
        assert VetoReason(reason)

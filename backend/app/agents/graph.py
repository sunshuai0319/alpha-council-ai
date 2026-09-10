import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.agents.llm import parse_json_response
from app.config import Settings, get_settings
from app.domain.enums import Action
from app.domain.schemas import AnalysisResult, TradeProposal, TradingCycleState


class CompletionClient(Protocol):
    def complete_json(self, prompt: str) -> str | dict[str, Any]: ...


class EvidenceRetriever(Protocol):
    def retrieve(self, query: str, *, asset: str | None = None, limit: int = 3) -> list[Any]: ...


class GraphState(TypedDict, total=False):
    tenant_id: str | None
    user_id: str
    cycle_id: str
    started_at: int
    symbol: str
    market_snapshot: dict[str, Any] | None
    candles_by_timeframe: dict[str, list[dict[str, Any]]]
    technical_indicators: dict[str, Any]
    news_items: list[dict[str, Any]]
    macro_events: list[dict[str, Any]]
    retrieved_evidence: list[dict[str, Any]]
    market_analysis: dict[str, Any] | None
    quant_analysis: dict[str, Any] | None
    macro_analysis: dict[str, Any] | None
    risk_assessment: dict[str, Any] | None
    trade_proposal: dict[str, Any] | None
    execution_result: dict[str, Any] | None
    errors: list[str]
    data_versions: dict[str, str]
    model_versions: dict[str, str]
    trace_ids: list[str]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_state(value: TradingCycleState | dict[str, Any]) -> TradingCycleState:
    return value if isinstance(value, TradingCycleState) else TradingCycleState.model_validate(value)


def _complete(llm: CompletionClient | Any, prompt: str) -> str | dict[str, Any]:
    if hasattr(llm, "complete_json"):
        return llm.complete_json(prompt)
    if hasattr(llm, "complete"):
        return llm.complete(prompt)
    if callable(llm):
        return llm(prompt)
    if hasattr(llm, "response"):
        return llm.response
    raise TypeError("configured agent LLM has no completion method")


def _hold_proposal(state: TradingCycleState, reason: str, now_ms: int) -> TradeProposal:
    return TradeProposal(
        proposal_id=f"hold-{state.cycle_id}",
        action=Action.HOLD,
        symbol=state.symbol,
        side=None,
        position_size_pct=0,
        leverage=1,
        stop_loss=None,
        take_profit=None,
        valid_until=now_ms,
        invalidation_conditions=[reason],
        confidence=0,
        reasoning_summary=reason,
        evidence_refs=[],
        model_version="safe-hold",
        trace_id=str(uuid4()),
    )


def _analysis(
    state: TradingCycleState,
    role: str,
    context: str,
    llm: CompletionClient | Any | None,
    now_ms: int,
) -> AnalysisResult:
    fallback = AnalysisResult(
        status="PROPOSED",
        confidence=0.2 if context else 0,
        reasoning_summary=f"{role} analysis is unavailable; defaulting to neutral.",
        evidence_refs=[],
        model_version="deterministic-fallback",
        trace_id=str(uuid4()),
    )
    if llm is None:
        return fallback
    prompt = (
        f"Role: {role}. Analyze symbol {state.symbol} at {now_ms}. "
        "Return JSON with status, confidence, reasoning_summary, evidence_refs, model_version, trace_id. "
        "Do not output orders or call tools.\n\nContext:\n" + context
    )
    try:
        result = AnalysisResult.model_validate(parse_json_response(_complete(llm, prompt)))
        return result
    except Exception:  # noqa: BLE001 - one analyst failure becomes a neutral analysis
        return fallback


def market_node(state: TradingCycleState, *, llm: CompletionClient | Any | None = None, now_ms: int | None = None) -> dict[str, Any]:
    current = _as_state(state)
    snapshot = current.market_snapshot.model_dump_json() if current.market_snapshot else "missing"
    result = _analysis(current, "Market Agent", snapshot, llm, now_ms or _now_ms())
    return {"market_analysis": result.model_dump()}


def quant_node(state: TradingCycleState, *, llm: CompletionClient | Any | None = None, now_ms: int | None = None) -> dict[str, Any]:
    current = _as_state(state)
    candle_context = json.dumps(
        {
            "indicators": current.technical_indicators,
            "candles": {
                timeframe: [candle.model_dump() for candle in candles[-20:]]
                for timeframe, candles in current.candles_by_timeframe.items()
            },
        },
        default=str,
    )
    result = _analysis(current, "Quant Agent", candle_context, llm, now_ms or _now_ms())
    return {"quant_analysis": result.model_dump()}


def macro_node(state: TradingCycleState, *, llm: CompletionClient | Any | None = None, now_ms: int | None = None) -> dict[str, Any]:
    current = _as_state(state)
    context = json.dumps(current.macro_events[-20:], default=str)
    result = _analysis(current, "Macro Agent", context, llm, now_ms or _now_ms())
    return {"macro_analysis": result.model_dump()}


def load_context(state: TradingCycleState, *, settings: Settings | None = None) -> dict[str, Any]:
    current = _as_state(state)
    configured = settings or get_settings()
    return {
        "model_versions": {**current.model_versions, "committee": configured.ark_model},
        "trace_ids": [*current.trace_ids, str(uuid4())],
    }


def validate_freshness(
    state: TradingCycleState,
    *,
    now_ms: int | None = None,
    max_age_seconds: int | None = None,
) -> dict[str, Any]:
    current = _as_state(state)
    now = now_ms or _now_ms()
    max_age = max_age_seconds if max_age_seconds is not None else get_settings().market_data_max_age_seconds
    errors = list(current.errors)
    if current.market_snapshot is None:
        errors.append("market_snapshot_missing")
    elif now - current.market_snapshot.captured_at > max_age * 1000:
        errors.append("market_snapshot_stale")
    return {"errors": errors}


def retrieve_evidence(state: TradingCycleState, *, retriever: EvidenceRetriever | None = None) -> dict[str, Any]:
    current = _as_state(state)
    if retriever is None:
        return {"retrieved_evidence": []}
    asset = current.symbol.split("-")[0].upper()
    try:
        evidence = retriever.retrieve(f"{asset} market outlook", asset=asset, limit=5)
        return {
            "retrieved_evidence": [item.__dict__ if hasattr(item, "__dict__") else item for item in evidence]
        }
    except Exception as exc:  # noqa: BLE001 - RAG failure must not create a trade
        return {"retrieved_evidence": [], "errors": [*current.errors, f"retrieval_failed:{exc}"]}


def run_committee(state: TradingCycleState, llm: CompletionClient | Any) -> TradeProposal:
    current = _as_state(state)
    prompt = (
        "You are the Committee Agent. Combine the three analyses and evidence into one trade proposal. "
        "Return JSON only with proposal_id, action (HOLD/LONG/SHORT/CLOSE), symbol, side, "
        "position_size_pct, leverage, stop_loss, take_profit, valid_until, invalidation_conditions, "
        "confidence, reasoning_summary, evidence_refs, model_version, trace_id. "
        "HOLD if evidence is missing, conflicting, stale, or insufficient. "
        "Real-time price and account values are not retrieved from RAG. Never call a trading tool.\n\n"
        + json.dumps(
            {
                "symbol": current.symbol,
                "market": current.market_analysis.model_dump() if current.market_analysis else None,
                "quant": current.quant_analysis.model_dump() if current.quant_analysis else None,
                "macro": current.macro_analysis.model_dump() if current.macro_analysis else None,
                "evidence": current.retrieved_evidence,
            },
            default=str,
        )
    )
    try:
        proposal = TradeProposal.model_validate(parse_json_response(_complete(llm, prompt)))
        if proposal.symbol.replace("-", "").upper() != current.symbol.replace("-", "").upper():
            raise ValueError("committee symbol does not match cycle symbol")
        return proposal
    except Exception:  # noqa: BLE001 - malformed or unsafe committee output is HOLD
        return _hold_proposal(current, "committee_invalid_json_or_schema", _now_ms())


def committee_node(
    state: TradingCycleState,
    *,
    llm: CompletionClient | Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = _as_state(state)
    if llm is None:
        proposal = _hold_proposal(current, "committee_llm_not_configured", now_ms or _now_ms())
    else:
        proposal = run_committee(current, llm)
    return {"trade_proposal": proposal.model_dump()}


def proposal_validator(state: TradingCycleState, *, now_ms: int | None = None) -> dict[str, Any]:
    current = _as_state(state)
    proposal = current.trade_proposal
    if proposal is None:
        return {"errors": [*current.errors, "proposal_missing"]}
    reasons: list[str] = []
    if proposal.symbol.replace("-", "").upper() != current.symbol.replace("-", "").upper():
        reasons.append("proposal_symbol_mismatch")
    if proposal.action in {Action.LONG, Action.SHORT}:
        if proposal.position_size_pct <= 0:
            reasons.append("entry_position_size_missing")
        if proposal.stop_loss is None:
            reasons.append("entry_stop_loss_missing")
        if not proposal.evidence_refs or not current.retrieved_evidence:
            reasons.append("entry_evidence_missing")
    if proposal.valid_until < (now_ms or _now_ms()) and proposal.action is not Action.HOLD:
        reasons.append("proposal_expired")
    return {"errors": [*current.errors, *reasons]}


def safe_hold(state: TradingCycleState, *, now_ms: int | None = None) -> dict[str, Any]:
    current = _as_state(state)
    reason = ";".join(current.errors[-3:]) or "safety_condition"
    return {"trade_proposal": _hold_proposal(current, reason, now_ms or _now_ms()).model_dump()}


class TradingCycleGraph:
    def __init__(self, compiled: Any, node_names: set[str]) -> None:
        self._compiled = compiled
        self.node_names = node_names

    def invoke(self, state: TradingCycleState | dict[str, Any]) -> TradingCycleState:
        raw = state.model_dump() if isinstance(state, TradingCycleState) else state
        return TradingCycleState.model_validate(self._compiled.invoke(raw))


def build_trading_cycle_graph(
    *,
    llm: CompletionClient | Any | None = None,
    retriever: EvidenceRetriever | None = None,
    settings: Settings | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> TradingCycleGraph:
    configured = settings or get_settings()
    now = clock_ms or _now_ms
    builder = StateGraph(GraphState)
    builder.add_node("load_context", lambda state: load_context(state, settings=configured))
    builder.add_node(
        "validate_freshness",
        lambda state: validate_freshness(state, now_ms=now(), max_age_seconds=configured.market_data_max_age_seconds),
    )
    builder.add_node("fanout", lambda _: {})
    builder.add_node("market_node", lambda state: market_node(state, llm=llm, now_ms=now()))
    builder.add_node("quant_node", lambda state: quant_node(state, llm=llm, now_ms=now()))
    builder.add_node("macro_node", lambda state: macro_node(state, llm=llm, now_ms=now()))
    builder.add_node("retrieve_evidence", lambda state: retrieve_evidence(state, retriever=retriever))
    builder.add_node("committee_node", lambda state: committee_node(state, llm=llm, now_ms=now()))
    builder.add_node("proposal_validator", lambda state: proposal_validator(state, now_ms=now()))
    builder.add_node("safe_hold", lambda state: safe_hold(state, now_ms=now()))
    builder.add_node("persist_decision", lambda state: {"data_versions": {**state.get("data_versions", {}), "cycle": "v1"}})
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "validate_freshness")
    builder.add_conditional_edges(
        "validate_freshness",
        lambda state: "safe_hold" if state.get("errors") else "fanout",
        {"safe_hold": "safe_hold", "fanout": "fanout"},
    )
    for node in ("market_node", "quant_node", "macro_node"):
        builder.add_edge("fanout", node)
        builder.add_edge(node, "retrieve_evidence")
    builder.add_edge("retrieve_evidence", "committee_node")
    builder.add_edge("committee_node", "proposal_validator")
    builder.add_conditional_edges(
        "proposal_validator",
        lambda state: "safe_hold" if state.get("errors") else "persist_decision",
        {"safe_hold": "safe_hold", "persist_decision": "persist_decision"},
    )
    builder.add_edge("safe_hold", "persist_decision")
    builder.add_edge("persist_decision", END)
    compiled = builder.compile()
    return TradingCycleGraph(
        compiled,
        {
            "load_context",
            "validate_freshness",
            "market_node",
            "quant_node",
            "macro_node",
            "retrieve_evidence",
            "committee_node",
            "proposal_validator",
            "safe_hold",
            "persist_decision",
        },
    )

import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Any, Protocol, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.agents.llm import parse_json_response
from app.config import Settings, get_settings
from app.domain.enums import Action
from app.domain.schemas import (
    AnalysisResult,
    TradeProposal,
    TradingCycleState,
    VetoReason,
    VetoVerdict,
)
from app.signals.scorer import HOLD as SIGNAL_HOLD
from app.signals.scorer import LONG as SIGNAL_LONG
from app.signals.scorer import score_signal
from app.signals.sizing import size_position


def _merge_verdicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """并行 veto agent 各写自己那一格，靠这个 reducer 合并不冲突。"""

    return {**left, **right}


class CompletionClient(Protocol):
    def complete_json(self, prompt: str) -> str | dict[str, Any]: ...


logger = logging.getLogger(__name__)


class EvidenceRetriever(Protocol):
    def retrieve(self, query: str, *, asset: str | None = None, limit: int = 3) -> list[Any]: ...


class GraphState(TypedDict, total=False):
    tenant_id: str | None
    user_id: str
    cycle_id: str
    started_at: int
    symbol: str
    market_snapshot: dict[str, Any] | None
    microstructure: dict[str, Any] | None
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
    signal_score: float | None
    veto_type: str | None
    #: 并行 veto agent 各写自己那一格，靠 _merge_verdicts 合并。
    veto_verdicts: Annotated[dict[str, Any], _merge_verdicts]
    equity: Decimal | None
    signal_decided_at: int | None
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


def _language_instruction(locale: str) -> str:
    """让 LLM 用与用户界面一致的语言写人类可读文本。

    JSON 字段名和枚举值（HOLD/LONG/BEARISH 等）保持英文，程序照常解析。
    """

    if locale.lower().startswith("zh"):
        return (
            " Write every human-readable text field (reasoning_summary, invalidation_conditions, "
            "evidence descriptions) in Simplified Chinese (简体中文). Keep JSON keys and enum "
            "values in English."
        )
    return ""


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
        "Do not output orders or call tools."
        + _language_instruction(state.locale)
        + "\n\nContext:\n" + context
    )
    try:
        result = AnalysisResult.model_validate(parse_json_response(_complete(llm, prompt)))
        return result
    except Exception as exc:  # noqa: BLE001 - one analyst failure becomes a neutral analysis
        logger.warning(
            "%s output rejected (%s), defaulting to neutral: %s",
            role,
            type(exc).__name__,
            str(exc)[:300],
        )
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


def signal_node(
    state: TradingCycleState,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """确定性规则信号器：方向 + 仓位 + SL/TP 全由规则定，不调 LLM。

    输出 TradeProposal。HOLD 时直接给出 hold 提案，后续走 persist 收尾，整条
    路径零 LLM 调用 —— 这是「不再全部 HOLD」的关键：方向不再靠散文分析猜。
    """

    current = _as_state(state)
    now = now_ms or _now_ms()
    direction, composite = score_signal(current.technical_indicators or {})
    if direction == SIGNAL_HOLD:
        return {
            "trade_proposal": _hold_proposal(current, f"signal_hold_score_{composite:.2f}", now).model_dump(),
            "signal_score": composite,
            "signal_decided_at": now,
        }
    snapshot = current.market_snapshot
    one_h = (current.technical_indicators or {}).get("1h") or {}
    atr = one_h.get("atr_14")
    if snapshot is None or snapshot.last_price <= 0 or atr is None or atr <= 0:
        # 缺 ATR / 价格：方向有了但没有风控所需的距离，宁可 HOLD。
        return {
            "trade_proposal": _hold_proposal(current, "signal_missing_atr_or_price", now).model_dump(),
            "signal_score": composite,
            "signal_decided_at": now,
        }
    # 仓位由风险预算反推，不依赖实际 equity —— position_size_pct 是比例，
    # equity 只用于把比例换算成名义金额，cycle 层再用真实余额算。
    side = SIGNAL_LONG if direction == SIGNAL_LONG else "SHORT"
    plan = size_position(
        equity=current.equity or Decimal(1),
        entry=Decimal(str(snapshot.last_price)),
        atr=Decimal(str(atr)),
        side=side,
    )
    proposal = TradeProposal(
        proposal_id=f"signal-{current.cycle_id}",
        action=Action.LONG if direction == SIGNAL_LONG else Action.SHORT,
        symbol=current.symbol,
        side=side,
        position_size_pct=float(plan.position_size_pct),
        leverage=1,
        stop_loss=float(plan.stop_loss),
        take_profit=float(plan.take_profit),
        # 交易所侧只挂这条宽的；紧的那条（stop_loss）由软件层管。
        disaster_stop=float(plan.disaster_stop),
        valid_until=now + 300_000,  # 5 分钟决策窗口，与周期对齐
        invalidation_conditions=[
            f"signal_score_{composite:.2f}",
            f"stop_{plan.stop_loss}",
        ],
        confidence=min(abs(composite), 1.0),
        reasoning_summary=f"rule signal {direction} score={composite:.2f}",
        evidence_refs=["indicators.1h.trend", "indicators.4h.trend"],
        model_version="rule-signal-v1",
        trace_id=str(uuid4()),
    )
    return {
        "trade_proposal": proposal.model_dump(),
        "signal_score": composite,
        "signal_decided_at": now,
    }


def _run_veto_agent(
    state: TradingCycleState,
    *,
    agent: str,
    charter: str,
    allowed_reasons: tuple[VetoReason, ...],
    context: dict[str, Any],
    llm: CompletionClient | Any | None,
) -> dict[str, Any]:
    """跑一个专业 veto agent，把它那一格写进 veto_verdicts。

    每个 agent 拿到的是**自己域内的数据**，这是「独立判断」的关键 —— 两个 agent
    读同一批数据、只是 prompt 措辞不同，那不叫独立。
    """

    if llm is None:
        # 没配 LLM 就不能否决：规则信号直接放行，不因缺 LLM 变 HOLD。
        return {"veto_verdicts": {agent: {"veto": False, "status": "skipped_no_llm"}}}
    reason_list = ", ".join(reason.value for reason in allowed_reasons)
    prompt = (
        f"You are the {agent} on a crypto trading desk. A rule-based signal proposes a trade. "
        f"Your charter, and ONLY this: {charter} "
        f"You may veto only for these reasons: {reason_list}. "
        "You must NOT change direction, size, or stop, and you must not comment outside your charter. "
        "`Not enough evidence` is NOT a valid reason — the rule signal already holds when evidence is thin. "
        "If you veto, evidence_refs must cite at least one real item from the data below. "
        "Return JSON with veto (true/false), reasons (list), evidence_refs (list), reasoning_summary."
        + _language_instruction(state.locale)
        + "\n\n"
        + json.dumps({"symbol": state.symbol, "proposed": state.trade_proposal.model_dump() if state.trade_proposal else None, **context}, default=str)
    )
    try:
        verdict = VetoVerdict.model_validate(parse_json_response(_complete(llm, prompt)))
    except Exception as exc:  # noqa: BLE001 - 输出非法 = 放行 + 计数，绝不因 LLM 格式错而拦单
        logger.warning(
            "%s veto output rejected (%s), counting as invalid_ignored: %s",
            agent,
            type(exc).__name__,
            str(exc)[:300],
        )
        return {"veto_verdicts": {agent: {"veto": False, "status": "invalid_ignored"}}}
    out_of_charter = [reason.value for reason in verdict.reasons if reason not in allowed_reasons]
    if out_of_charter:
        # 用别的域的否决理由 = 越界。接受它会让「按域独立」失去意义，也会污染
        # 后续按 agent 统计的否决精度 —— 记成无效放行。
        logger.warning("%s vetoed outside its charter (%s), ignoring", agent, out_of_charter)
        return {
            "veto_verdicts": {
                agent: {"veto": False, "status": "invalid_ignored", "out_of_charter": out_of_charter}
            }
        }
    return {
        "veto_verdicts": {
            agent: {
                "veto": verdict.veto,
                "status": "applied" if verdict.veto else "none",
                "reasons": [reason.value for reason in verdict.reasons],
                "evidence_refs": verdict.evidence_refs,
                "reasoning_summary": verdict.reasoning_summary,
            }
        }
    }


def news_veto_node(
    state: TradingCycleState,
    *,
    llm: CompletionClient | Any | None = None,
) -> dict[str, Any]:
    """新闻/宏观域：有没有新事件与本单方向冲突。"""

    current = _as_state(state)
    if _proposal_is_hold(current.model_dump()):
        return {}
    return _run_veto_agent(
        current,
        agent="news_macro",
        charter=(
            "Judge whether fresh news or macro events directly contradict this trade's direction, "
            "or whether the prevailing regime makes it unsafe. Ignore technical structure — another agent covers that."
        ),
        allowed_reasons=(VetoReason.NEWS_SHOCK, VetoReason.REGIME_CONFLICT),
        context={
            "evidence": current.retrieved_evidence,
            "macro_events": current.macro_events,
        },
        llm=llm,
    )


def structure_veto_node(
    state: TradingCycleState,
    *,
    llm: CompletionClient | Any | None = None,
) -> dict[str, Any]:
    """结构/流动性域：信号依赖的技术结构是否已破坏，盘口是否异常。"""

    current = _as_state(state)
    if _proposal_is_hold(current.model_dump()):
        return {}
    micro = current.microstructure
    return _run_veto_agent(
        current,
        agent="structure_liquidity",
        charter=(
            "Judge whether the technical structure the signal relied on is already invalidated, "
            "or whether the order book shows an anomaly that makes execution unsafe "
            "(crossed or absurdly wide spread, lopsided depth). Ignore news — another agent covers that."
        ),
        allowed_reasons=(VetoReason.STRUCTURE_INVALIDATED, VetoReason.LIQUIDITY_ANOMALY),
        context={
            "indicators": current.technical_indicators,
            "microstructure": micro.model_dump() if micro else None,
        },
        llm=llm,
    )


#: 价差超过这个值（基点）说明盘口已经异常，不值得在这个价位执行。
MAX_SPREAD_BPS = 50.0


def data_integrity_node(state: TradingCycleState) -> dict[str, Any]:
    """数据完整性 —— 确定性检查，不调 LLM。

    这是事实判断不是语境判断：价差倒挂、24h 区间倒置都是可以算出来的，让 LLM
    再看一遍只会增加延迟和不确定性。

    只在**观测到**异常时否决；数据缺失不算异常（spec 2.1：缺项不该整体 HOLD）。
    """

    current = _as_state(state)
    if _proposal_is_hold(current.model_dump()):
        return {}
    problems: list[str] = []
    micro = current.microstructure
    if micro is not None:
        if micro.spread_bps is not None and micro.spread_bps > MAX_SPREAD_BPS:
            problems.append(f"spread_bps={micro.spread_bps:.1f}>{MAX_SPREAD_BPS}")
        if micro.bid is not None and micro.ask is not None and micro.ask < micro.bid:
            problems.append("crossed_book")
    snapshot = current.market_snapshot
    if snapshot is not None and snapshot.high_24h is not None and snapshot.low_24h is not None:
        if snapshot.high_24h < snapshot.low_24h:
            problems.append("inverted_24h_range")
        elif snapshot.last_price > 0 and not (snapshot.low_24h <= snapshot.last_price <= snapshot.high_24h):
            problems.append("last_price_outside_24h_range")
    if not problems:
        return {"veto_verdicts": {"data_integrity": {"veto": False, "status": "none"}}}
    return {
        "veto_verdicts": {
            "data_integrity": {
                "veto": True,
                "status": "applied",
                "reasons": [VetoReason.DATA_INTEGRITY.value],
                "evidence_refs": problems,
                "reasoning_summary": "; ".join(problems),
            }
        }
    }


def merge_veto_node(
    state: TradingCycleState,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """合并各 agent 的结论：**任一 veto 即拦截**。

    多数表决在这里是错的 —— veto 是安全机制，一个域发现真问题就该拦，不该被
    另外两个「没发现问题」的 agent 投票盖过。
    """

    current = _as_state(state)
    now = now_ms or _now_ms()
    verdicts = current.veto_verdicts or {}
    applied = {name: v for name, v in verdicts.items() if v.get("veto")}
    invalid = {name: v for name, v in verdicts.items() if v.get("status") == "invalid_ignored"}
    if applied:
        reasons = ",".join(
            f"{name}:{','.join(v.get('reasons') or ['veto'])}" for name, v in applied.items()
        )
        return {
            "trade_proposal": _hold_proposal(current, f"vetoed:{reasons}", now).model_dump(),
            "veto_type": "veto_applied",
        }
    if invalid:
        return {"veto_type": "veto_invalid_ignored"}
    return {"veto_type": "veto_none"}


def run_committee(state: TradingCycleState, llm: CompletionClient | Any) -> TradeProposal:
    current = _as_state(state)
    prompt = (
        "You are the Committee Agent. Combine the three analyses and evidence into one trade proposal. "
        "Return JSON only with proposal_id, action (HOLD/LONG/SHORT/CLOSE), symbol, side, "
        "position_size_pct, leverage, stop_loss, take_profit, valid_until, invalidation_conditions, "
        "confidence, reasoning_summary, evidence_refs, model_version, trace_id. "
        "HOLD if evidence is missing, conflicting, stale, or insufficient. "
        "Real-time price and account values are not retrieved from RAG. Never call a trading tool."
        + _language_instruction(current.locale)
        + "\n\n"
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
    except Exception as exc:  # noqa: BLE001 - malformed or unsafe committee output is HOLD
        # 记录具体原因：这条 fallback 过去只留一个笼统标记，排查时只能靠复现。
        logger.warning(
            "committee output rejected (%s), falling back to hold: %s",
            type(exc).__name__,
            str(exc)[:500],
        )
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
    valid_until = proposal.valid_until
    if (
        valid_until is not None
        and valid_until < (now_ms or _now_ms())
        and proposal.action is not Action.HOLD
    ):
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


def _proposal_is_hold(state: dict[str, Any]) -> bool:
    proposal = state.get("trade_proposal")
    return not proposal or str(proposal.get("action", "HOLD")).upper() == "HOLD"


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
    builder.add_node("signal_node", lambda state: signal_node(state, now_ms=now()))
    builder.add_node("retrieve_evidence", lambda state: retrieve_evidence(state, retriever=retriever))
    builder.add_node("veto_fanout", lambda _: {})
    builder.add_node("news_veto_node", lambda state: news_veto_node(state, llm=llm))
    builder.add_node("structure_veto_node", lambda state: structure_veto_node(state, llm=llm))
    builder.add_node("data_integrity_node", data_integrity_node)
    builder.add_node("merge_veto", lambda state: merge_veto_node(state, now_ms=now()))
    builder.add_node("proposal_validator", lambda state: proposal_validator(state, now_ms=now()))
    builder.add_node("safe_hold", lambda state: safe_hold(state, now_ms=now()))
    builder.add_node("persist_decision", lambda state: {"data_versions": {**state.get("data_versions", {}), "cycle": "v1"}})
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "validate_freshness")
    builder.add_conditional_edges(
        "validate_freshness",
        lambda state: "safe_hold" if state.get("errors") else "signal_node",
        {"safe_hold": "safe_hold", "signal_node": "signal_node"},
    )
    # HOLD 提案直接收尾：方向已定、无可否决，整条路径零 LLM 调用。
    # 非 HOLD 才做 RAG 检索 + LLM 否决。
    builder.add_conditional_edges(
        "signal_node",
        lambda state: "persist_decision" if _proposal_is_hold(state) else "retrieve_evidence",
        {"persist_decision": "persist_decision", "retrieve_evidence": "retrieve_evidence"},
    )
    # 三个专业 veto 并行跑、扇入到 merge：任一 veto 即拦截（见 merge_veto_node）。
    builder.add_edge("retrieve_evidence", "veto_fanout")
    for veto_node_name in ("news_veto_node", "structure_veto_node", "data_integrity_node"):
        builder.add_edge("veto_fanout", veto_node_name)
        builder.add_edge(veto_node_name, "merge_veto")
    builder.add_edge("merge_veto", "proposal_validator")
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
            "signal_node",
            "retrieve_evidence",
            "veto_fanout",
            "news_veto_node",
            "structure_veto_node",
            "data_integrity_node",
            "merge_veto",
            "proposal_validator",
            "safe_hold",
            "persist_decision",
        },
    )

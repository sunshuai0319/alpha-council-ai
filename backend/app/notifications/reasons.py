"""后端机器码 → 卡片中文文案。

后端落库的是**稳定的英文机器码**（`position_already_open`、
`rule signal SHORT score=-0.35`…）—— 那是审计和查询要用的标识，不该为了显示而本地化
（理由见 `frontend/lib/labels.ts` 顶部说明）。网页端早就有翻译层，卡片漏了，补在这里。

措辞与 `frontend/lib/i18n.tsx` 的 zh-CN 逐字对齐：同一份决策，网页和卡片要看到同一句话，
否则排查时得在两套说法之间做心算映射。

认不出的码**原样显示**，不要吞掉 —— 展示一个陌生码远好过显示空白，而且它本身
就是排查线索。
"""

from __future__ import annotations

import re

#: 精确匹配的码。与 `frontend/lib/labels.ts` 的 `REASON_KEYS` 一一对应。
_REASON_TEXTS = {
    # 观望与账户状态
    "hold_no_order": "规则信号未达开仓条件，未下单",
    "paused": "账户已暂停",
    "trading_disabled": "全局交易开关已关闭",
    # 行情与账户可用性
    "market_data_stale": "行情数据过期",
    "market_snapshot_missing": "缺少行情快照",
    "market_snapshot_stale": "行情快照过期",
    "account_or_market_missing": "账户或行情数据缺失",
    "proposal_missing": "缺少交易提案",
    "entry_non_positive": "入场价异常",
    # 风控拒绝
    "position_already_open": "该品种已有持仓，不再加仓",
    "max_notional": "账户总敞口超限",
    "max_position_notional": "单笔名义敞口超限",
    "max_leverage": "杠杆超出上限",
    "single_trade_risk": "单笔风险超出预算",
    "stop_loss_required": "缺少止损",
    "daily_trade_limit": "当日开仓额度已用尽",
    "daily_loss_limit": "当日亏损触发熔断",
    "consecutive_loss_cooldown": "连续亏损触发熔断",
    "equity_non_positive": "账户权益非正",
    "long_stop_must_be_below_entry": "做多止损必须低于入场价",
    "short_stop_must_be_above_entry": "做空止损必须高于入场价",
    "long_take_profit_must_be_above_entry": "做多止盈必须高于入场价",
    "short_take_profit_must_be_below_entry": "做空止盈必须低于入场价",
    "reward_risk_too_low": "盈亏比低于下限",
    "entry_position_size_missing": "缺少仓位大小",
    # 交易提案校验（`agents/graph.py` 的 validate 节点）—— 与 risk engine 的
    # `stop_loss_required` 不同：那条是「风控不放行」，这条是「提案本身不完整」。
    "entry_stop_loss_missing": "提案缺少止损",
    "entry_signal_evidence_missing": "提案缺少入场证据引用",
    # 本地账本与交易所对账 / 再入场冷却（`services/cycle.py`）
    "exchange_position_missing": "本地有持仓、交易所无，暂不开新仓",
    "reentry_cooldown": "刚平仓，冷却期内不开新仓",
    "manual_reduce_only": "人工减仓",
    "proposal_expired": "提案已过期",
    "proposal_symbol_mismatch": "提案品种与周期不符",
    # 数据完整性分支（`agents/graph.py` 的 data_integrity_node）：这些是**算出来的**
    # 异常，写进 evidence 与 reasoning_summary，智囊团页照着渲染。
    "crossed_book": "盘口倒挂（买价高于卖价）",
    "inverted_24h_range": "24 小时最高价低于最低价",
    "last_price_outside_24h_range": "最新价落在 24 小时区间之外",
    # 委员会 / 信号器
    "committee_invalid_json_or_schema": "模型输出无法解析，已退化为观望",
    "committee_llm_not_configured": "未配置模型，已退化为观望",
    "signal_missing_atr_or_price": "缺少 ATR 或价格，无法定止损",
    "safe_hold": "安全观望",
    "repeated_cycle_failures": "连续周期失败触发熔断",
    "market_data_unavailable": "行情获取失败",
}

#: 带参数的码：前缀 → 模板。与 `frontend/lib/labels.ts` 的 `PREFIX_KEYS` 对应。
_REASON_TEMPLATES = {
    "signal_hold_score_": "信号分 {score}，未达开仓阈值",
    "market_data_unavailable:": "行情获取失败：{detail}",
    "account_unavailable:": "账户不可用：{detail}",
    "retrieval_failed:": "证据检索失败：{detail}",
    "vetoed:": "被否决：{detail}",
}

#: 规则信号器开仓时的摘要，与 `frontend/lib/labels.ts` 的 `RULE_SIGNAL` 同源。
_RULE_SIGNAL = re.compile(r"^rule signal (LONG|SHORT) score=(-?[\d.]+)$")

#: 盘口点差超标（`agents/graph.py` 的 `f"spread_bps={value:.1f}>{MAX_SPREAD_BPS}"`）。
#: 是个 f-string 而不是枚举码，所以走正则而不是前缀表。
_SPREAD_TOO_WIDE = re.compile(r"^spread_bps=([\d.]+)>([\d.]+)$")

#: 卡片通篇无英文，方向用中文 —— 表头标签本来也是「做空」。
_DIRECTIONS = {"LONG": "做多", "SHORT": "做空"}


def _format_score(value: str) -> str:
    """分数统一两位小数并去掉尾随零：0.26000000000001 → 0.26。

    小数点保证去尾零不会吃进整数部分，nan/inf 也照常渲染成 `nan`/`inf`。
    """

    try:
        rendered = f"{float(value):.2f}"
    except ValueError:
        return value
    return rendered.rstrip("0").rstrip(".") or "0"


def reason_text(code: str) -> str:
    """把一条后端码翻成中文；认不出来就原样返回。"""

    if not code:
        return code
    exact = _REASON_TEXTS.get(code)
    if exact is not None:
        return exact
    entry = _RULE_SIGNAL.match(code)
    if entry is not None:
        return f"规则信号 {_DIRECTIONS[entry.group(1)]}，分数 {_format_score(entry.group(2))}"
    spread = _SPREAD_TOO_WIDE.match(code)
    if spread is not None:
        return f"盘口点差 {spread.group(1)} bps，超过 {spread.group(2)} bps 上限"
    for prefix, template in _REASON_TEMPLATES.items():
        if code.startswith(prefix):
            detail = code[len(prefix) :]
            if prefix == "signal_hold_score_":
                return template.format(score=_format_score(detail))
            return template.format(detail=detail)
    return code


def reason_parts(reason: str) -> str:
    """翻译一条可能由 `;` 拼起来的原因串，用「、」连回一句。

    后端有多处 `"; ".join(problems)`（`agents/graph.py`），所以按 `;` 拆开逐条翻译，
    并 strip 掉拼接时留下的空格，否则 `" hold_no_order"` 匹配不上精确表。
    """

    return "、".join(reason_text(part.strip()) for part in reason.split(";") if part.strip())

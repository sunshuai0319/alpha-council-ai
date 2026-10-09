"""卡片文案译表：后端机器码 → 中文。

措辞必须与 `frontend/lib/i18n.tsx` 的 zh-CN 一致 —— 同一份决策，网页和卡片
要看到同一句话，否则排查时得在两套说法之间做映射。
"""

from app.notifications.reasons import reason_parts, reason_text


def test_exact_code_is_translated() -> None:
    assert reason_text("position_already_open") == "该品种已有持仓，不再加仓"
    assert reason_text("daily_loss_limit") == "当日亏损触发熔断"
    assert reason_text("market_data_stale") == "行情数据过期"
    assert reason_text("hold_no_order") == "规则信号未达开仓条件，未下单"


def test_parameterized_code_keeps_its_argument() -> None:
    assert reason_text("market_data_unavailable:ETH-USDT/12h") == "行情获取失败：ETH-USDT/12h"
    assert reason_text("account_unavailable:connection refused") == "账户不可用：connection refused"


def test_signal_hold_score_is_rounded() -> None:
    assert reason_text("signal_hold_score_0.26000000000001") == "信号分 0.26，未达开仓阈值"
    assert reason_text("signal_hold_score_-0.5") == "信号分 -0.5，未达开仓阈值"


def test_rule_signal_summary_uses_chinese_direction() -> None:
    assert reason_text("rule signal SHORT score=-0.5477") == "规则信号 做空，分数 -0.55"
    assert reason_text("rule signal LONG score=0.31") == "规则信号 做多，分数 0.31"


def test_unknown_code_is_shown_as_is() -> None:
    # 认不出来就原样显示：陌生码远好过空白，而且它本身就是排查线索。
    assert reason_text("inverted_24h_range") == "inverted_24h_range"


def test_reason_parts_splits_on_semicolon() -> None:
    # 后端是 `"; ".join(problems)`，拼接留下的空格要 strip 掉，否则匹配不上精确表。
    assert (
        reason_parts("entry_evidence_missing; hold_no_order")
        == "缺少证据引用、规则信号未达开仓条件，未下单"
    )


def test_reason_parts_passes_through_free_text() -> None:
    # LLM 写的散文本来就已经是中文，不该被拆坏。
    assert reason_parts("趋势与动量一致，顺势做空") == "趋势与动量一致，顺势做空"


def test_empty_input_stays_empty() -> None:
    assert reason_text("") == ""
    assert reason_parts("") == ""

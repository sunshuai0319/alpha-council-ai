"""卡片文案译表：后端机器码 → 中文。

措辞必须与 `frontend/lib/i18n.tsx` 的 zh-CN 一致 —— 同一份决策，网页和卡片
要看到同一句话，否则排查时得在两套说法之间做映射。
"""

import re
from pathlib import Path

import pytest

from app.notifications.reasons import _REASON_TEMPLATES, _REASON_TEXTS, reason_parts, reason_text


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


def test_entry_and_position_codes_are_translated() -> None:
    """这一批是补出来的：审计发现卡片会把它们原样打成英文。"""

    assert reason_text("entry_stop_loss_missing") == "提案缺少止损"
    assert reason_text("entry_signal_evidence_missing") == "提案缺少入场证据引用"
    assert reason_text("exchange_position_missing") == "本地有持仓、交易所无，暂不开新仓"
    assert reason_text("reentry_cooldown") == "刚平仓，冷却期内不开新仓"
    assert reason_text("manual_reduce_only") == "人工减仓"


def test_data_integrity_codes_are_translated() -> None:
    """`data_integrity` 分支把具体问题写进 evidence，智囊团页会照着渲染。"""

    assert reason_text("crossed_book") == "盘口倒挂（买价高于卖价）"
    assert reason_text("inverted_24h_range") == "24 小时最高价低于最低价"
    assert reason_text("last_price_outside_24h_range") == "最新价落在 24 小时区间之外"
    assert reason_text("spread_bps=62.5>50.0") == "盘口点差 62.5 bps，超过 50.0 bps 上限"


def test_unknown_code_is_shown_as_is() -> None:
    # 认不出来就原样显示：陌生码远好过空白，而且它本身就是排查线索。
    assert reason_text("some_future_code") == "some_future_code"


def test_reason_parts_splits_on_semicolon() -> None:
    # 后端是 `"; ".join(problems)`，拼接留下的空格要 strip 掉，否则匹配不上精确表。
    assert (
        reason_parts("entry_signal_evidence_missing; hold_no_order")
        == "提案缺少入场证据引用、规则信号未达开仓条件，未下单"
    )


def test_reason_parts_passes_through_free_text() -> None:
    # LLM 写的散文本来就已经是中文，不该被拆坏。
    assert reason_parts("趋势与动量一致，顺势做空") == "趋势与动量一致，顺势做空"


def test_empty_input_stays_empty() -> None:
    assert reason_text("") == ""
    assert reason_parts("") == ""


# --- 与前端译表对齐 -------------------------------------------------------

_LABELS_TS = Path(__file__).resolve().parents[3] / "frontend" / "lib" / "labels.ts"


@pytest.mark.skipif(not _LABELS_TS.exists(), reason="只跑后端镜像时前端源码不在")
def test_card_table_never_exceeds_the_web_table() -> None:
    """卡片译表必须是网页译表的子集。

    译表分居 TS / Python 两侧、各自看着都「没问题」，所以这层对齐必须有人盯：
    曾经漏掉 5 个码，卡片和网页一起把机器码当正文显示。

    只查「有没有」，不查措辞 —— 措辞一致性靠 code review，不靠解析源码。
    """

    source = _LABELS_TS.read_text(encoding="utf-8")
    exact = re.search(r"const REASON_KEYS[^{]*\{(.*?)\n\}", source, flags=re.DOTALL)
    assert exact is not None, "labels.ts 里找不到 REASON_KEYS"
    known = set(re.findall(r"^\s+([\w]+):", exact.group(1), flags=re.MULTILINE))

    prefixes = re.search(r"const PREFIX_KEYS[^=]*=\s*\[(.*?)\n\]", source, flags=re.DOTALL)
    assert prefixes is not None, "labels.ts 里找不到 PREFIX_KEYS"
    known_prefixes = set(re.findall(r'\["([^"]+)"', prefixes.group(1)))

    assert set(_REASON_TEXTS) - known == set()
    assert set(_REASON_TEMPLATES) - known_prefixes == set()

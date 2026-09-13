"""持仓管理规则：保本 / 移动止损 / 时间止损 / 结构失效 / 有效止损触及。

设计成纯函数：给一份持仓状态，返回该做什么。不做 IO —— 下单留给 cycle 层。
R = |entry - initial_stop|，所有阈值都以 R 为单位。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.positions.manager import CLOSE, HOLD, manage

OPENED = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)


def _long(**overrides):
    """多仓：entry 100，初始止损 97 → R = 3。"""
    base = {
        "side": "LONG",
        "entry_price": Decimal(100),
        "initial_stop": Decimal(97),
        "effective_stop": Decimal(97),
        "take_profit": Decimal(106),
        "near_target_at": None,
        "peak_price": Decimal(100),
        "price": Decimal(100),
        "atr": Decimal(3),
        "opened_at": OPENED,
        "now": OPENED + timedelta(minutes=30),
        "signal_score": 0.5,
    }
    return manage(**{**base, **overrides})


def test_holds_while_nothing_triggers() -> None:
    decision = _long()
    assert decision.action == HOLD
    assert decision.effective_stop == Decimal(97)


def test_closes_when_price_touches_the_effective_stop() -> None:
    decision = _long(price=Decimal("96.9"))
    assert decision.action == CLOSE
    assert "stop" in decision.reason


def test_moves_stop_to_breakeven_at_one_r() -> None:
    """浮盈 ≥ 1R → 止损上移到入场价，这笔最差打平。"""
    decision = _long(price=Decimal(103), peak_price=Decimal(103))
    assert decision.action == HOLD
    assert decision.effective_stop == Decimal(100)


def test_trails_at_one_atr_once_past_one_r() -> None:
    """浮盈超 1R 后跟随 1×ATR：peak 106 → 止损 103。"""
    decision = _long(price=Decimal(105), peak_price=Decimal(105))
    assert decision.effective_stop == Decimal(102)


def test_closes_when_price_reaches_or_crosses_software_take_profit() -> None:
    """软件止盈必须使用 >=，不能因为 5 分钟采样跨过精确价而漏平。"""
    decision = _long(price=Decimal("106.1"), peak_price=Decimal("106.1"))

    assert decision.action == CLOSE
    assert decision.reason == "take_profit"


def test_latches_the_time_when_position_reaches_near_target() -> None:
    reached_at = OPENED + timedelta(hours=1)
    decision = _long(
        price=Decimal("105.5"),
        peak_price=Decimal("105.5"),
        now=reached_at,
    )

    assert decision.action == HOLD
    assert decision.near_target_at == reached_at


def test_closes_when_near_target_has_stalled_past_timeout() -> None:
    decision = _long(
        price=Decimal(104),
        peak_price=Decimal("105.5"),
        near_target_at=OPENED + timedelta(hours=1),
        now=OPENED + timedelta(hours=7),
    )

    assert decision.action == CLOSE
    assert decision.reason == "near_target_timeout"


def test_closes_any_position_past_absolute_max_hold() -> None:
    decision = _long(
        price=Decimal(101),
        now=OPENED + timedelta(hours=73),
    )

    assert decision.action == CLOSE
    assert decision.reason == "max_hold"


def test_short_closes_when_price_reaches_take_profit() -> None:
    decision = manage(
        side="SHORT",
        entry_price=Decimal(100),
        initial_stop=Decimal(103),
        effective_stop=Decimal(103),
        take_profit=Decimal(94),
        near_target_at=None,
        peak_price=Decimal(100),
        price=Decimal(94),
        atr=Decimal(3),
        opened_at=OPENED,
        now=OPENED + timedelta(minutes=30),
        signal_score=Decimal("-0.5"),
    )

    assert decision.action == CLOSE
    assert decision.reason == "take_profit"


def test_trailing_stop_never_moves_backwards() -> None:
    """价格回落不能把止损往下带 —— 只能上移。"""
    decision = _long(
        effective_stop=Decimal(103),
        price=Decimal(104),
        peak_price=Decimal(106),
    )
    assert decision.effective_stop == Decimal(103)


def test_breakeven_is_not_applied_before_one_r() -> None:
    """只到 0.5R 时不许动止损，否则正常回撤就被扫出去。"""
    decision = _long(price=Decimal("101.5"), peak_price=Decimal("101.5"))
    assert decision.effective_stop == Decimal(97)


def test_short_position_trails_downwards() -> None:
    """空仓的止损在上方，跟随方向相反。"""
    decision = manage(
        side="SHORT",
        entry_price=Decimal(100),
        initial_stop=Decimal(103),
        effective_stop=Decimal(103),
        take_profit=Decimal(90),
        near_target_at=None,
        peak_price=Decimal(94),
        price=Decimal(94),
        atr=Decimal(3),
        opened_at=OPENED,
        now=OPENED + timedelta(minutes=30),
        signal_score=Decimal("-0.5"),
    )
    assert decision.effective_stop == Decimal(97)


def test_tracks_the_peak_price() -> None:
    decision = _long(peak_price=Decimal(105), price=Decimal(104))
    assert decision.peak_price == Decimal(105)


def test_time_stop_closes_a_stalled_position() -> None:
    """持仓超 48 小时且浮盈 < 0.3R → 平掉，别让它占着风险预算。"""
    decision = _long(
        opened_at=OPENED,
        now=OPENED + timedelta(hours=49),
        price=Decimal("100.5"),  # 0.17R
        peak_price=Decimal("100.5"),
        atr=Decimal(3),
    )
    assert decision.action == CLOSE
    assert "time" in decision.reason


def test_time_stop_spares_a_position_still_working() -> None:
    """持仓久但有浮盈（≥ 0.3R）就不该被时间止损打掉。"""
    decision = _long(
        opened_at=OPENED,
        now=OPENED + timedelta(hours=49),
        price=Decimal(101),  # 0.33R
        peak_price=Decimal(101),
    )
    assert decision.action == HOLD


def test_structure_invalidation_closes_a_long_when_the_signal_flips() -> None:
    """规则分反向穿越 0 → 当初的理由没了，平掉。"""
    decision = _long(signal_score=Decimal("-0.1"))
    assert decision.action == CLOSE
    assert "structure" in decision.reason


def test_structure_invalidation_ignores_the_other_direction() -> None:
    """多仓不该被「依然偏多」的信号平掉。"""
    assert _long(signal_score=Decimal("0.9")).action == HOLD


def test_missing_signal_score_does_not_close() -> None:
    """算不出分数时不能当结构失效处理 —— 缺数据不是平仓理由。"""
    assert _long(signal_score=None).action == HOLD


def test_missing_atr_still_allows_breakeven_but_not_trailing() -> None:
    """没有 ATR 就算不出跟随距离，但保本仍应生效。"""
    decision = _long(atr=None, price=Decimal(104), peak_price=Decimal(104))
    assert decision.effective_stop == Decimal(100)


def test_stop_hit_takes_precedence_over_everything_else() -> None:
    """止损触及优先级最高，不该被结构/时间规则挡住。"""
    decision = _long(
        price=Decimal(96),
        effective_stop=Decimal(97),
        signal_score=Decimal("0.9"),
        opened_at=OPENED,
        now=OPENED + timedelta(hours=1),
    )
    assert decision.action == CLOSE
    assert "stop" in decision.reason


def test_time_stop_tolerates_a_naive_timestamp() -> None:
    """SQLite 取回的 timestamp 是 naive 的（Postgres 是 aware），相减不该炸。"""
    naive_opened = datetime(2026, 9, 11, 0, 0)  # 无 tzinfo
    decision = _long(
        opened_at=naive_opened,
        now=naive_opened.replace(tzinfo=UTC) + timedelta(hours=49),
        price=Decimal("100.5"),
        peak_price=Decimal("100.5"),
    )
    assert decision.action == CLOSE
    assert "time" in decision.reason

"""仓位与 SL/TP 由风险预算反推，不由 LLM 拍。

spec 2.2：stop_distance = 1.5 × ATR(1h)，notional = risk_budget / (stop_pct)，
封顶 20% 权益。SL/TP 由距离推导，灾难止损 = 3 × stop_distance。
"""

from decimal import Decimal

import pytest

from app.signals.sizing import PositionPlan, size_position


def _plan(**overrides) -> PositionPlan:
    base = {
        "equity": Decimal(10000),
        "entry": Decimal(100),
        "atr": Decimal(2),  # stop_distance = 1.5 × 2 = 3
        "side": "LONG",
    }
    return size_position(**{**base, **overrides})


def test_stop_distance_is_k_times_atr() -> None:
    plan = _plan()
    assert plan.stop_distance == Decimal(3)


def test_position_size_derives_from_risk_budget() -> None:
    """risk_budget = 0.5% × 10000 = 50；stop_pct = 3% → notional = 1666.67。

    cap = 20% × 10000 = 2000，未触发。
    """
    plan = _plan()
    # 1666.666.../10000 → 向下收敛 6 位小数
    assert plan.position_size_pct == Decimal("0.166666")
    assert plan.notional == Decimal("1666.666666")


def test_large_risk_caps_position_at_max_notional() -> None:
    """stop_distance 极小（ATR=0.1）→ 反推的 notional 超 20% 上限 → 封顶。"""
    plan = _plan(atr=Decimal("0.1"))
    assert plan.position_size_pct == Decimal("0.20")
    assert plan.notional == Decimal(2000)


def test_sl_and_tp_are_placed_by_side() -> None:
    long_plan = _plan()
    assert long_plan.stop_loss == Decimal(97)
    assert long_plan.take_profit == Decimal(106)

    short_plan = _plan(side="SHORT")
    assert short_plan.stop_loss == Decimal(103)
    assert short_plan.take_profit == Decimal(94)


def test_disaster_stop_is_three_stop_distances() -> None:
    """灾难止损是唯一挂交易所侧的宽止损（spec 3.3）。"""
    plan = _plan()
    assert plan.disaster_stop == Decimal(91)


def test_rejects_non_positive_atr_or_entry() -> None:
    with pytest.raises(ValueError):
        _plan(atr=Decimal(0))
    with pytest.raises(ValueError):
        _plan(entry=Decimal(0))

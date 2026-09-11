"""参数扫描：看的是**稳健性**，不是找最优。

在 41 天样本上挑「最优参数」就是过拟合。有意义的问题是：结果对参数是不是极度
敏感？如果相邻取值之间天差地别，那多半是噪声拟合出来的，换一段时间就失效。
"""

from decimal import Decimal

from app.backtest.sweep import robustness, sweep
from app.signals.params import StrategyParams
from tests.unit.test_backtest import _frame, _uptrend


def _frame_with_trades():
    """一段有信号、也有反向波动的行情，保证扫出来的结果不是全零。"""

    return _frame(_uptrend(160) + [124.0 + index * 1.2 for index in range(40)])


def test_sweep_covers_the_cartesian_product_of_the_axes() -> None:
    points = sweep(
        _frame_with_trades(),
        entry_thresholds=(0.25, 0.35),
        atr_multipliers=(Decimal("1.0"), Decimal("1.5")),
        reward_risks=(Decimal("2.0"),),
    )

    assert len(points) == 2 * 2 * 1
    seen = {(p.params.entry_threshold, p.params.atr_multiplier) for p in points}
    assert seen == {(0.25, Decimal("1.0")), (0.25, Decimal("1.5")),
                    (0.35, Decimal("1.0")), (0.35, Decimal("1.5"))}


def test_sweep_keeps_the_base_params_it_was_not_sweeping() -> None:
    base = StrategyParams(time_stop_hours=12, volume_weight=0.5)
    points = sweep(
        _frame_with_trades(),
        entry_thresholds=(0.35,),
        atr_multipliers=(Decimal("1.5"),),
        reward_risks=(Decimal("2.0"),),
        base=base,
    )

    assert points[0].params.time_stop_hours == 12
    assert points[0].params.volume_weight == 0.5


def test_sweep_is_sorted_by_expectancy_so_the_top_is_easy_to_spot() -> None:
    points = sweep(
        _frame_with_trades(),
        entry_thresholds=(0.25, 0.35, 0.6),
        atr_multipliers=(Decimal("1.5"),),
        reward_risks=(Decimal("2.0"),),
    )

    expectancies = [p.result.avg_r for p in points]
    assert expectancies == sorted(expectancies, reverse=True)


def test_robustness_reports_the_spread_on_each_axis() -> None:
    """每个轴上平均 R 的极差 —— 极差大就说明这套参数不稳。"""

    points = sweep(
        _frame_with_trades(),
        entry_thresholds=(0.25, 0.35, 0.6),
        atr_multipliers=(Decimal("1.5"),),
        reward_risks=(Decimal("2.0"),),
    )
    report = robustness(points)

    assert "entry_threshold" in report
    axis = report["entry_threshold"]
    assert axis["min"] <= axis["max"]
    assert axis["spread"] == axis["max"] - axis["min"]


def test_a_higher_threshold_produces_fewer_or_equal_trades() -> None:
    """阈值越高越难触发 —— 这是单调性，不该受噪声影响。"""

    frame = _frame_with_trades()
    loose = sweep(frame, entry_thresholds=(0.2,), atr_multipliers=(Decimal("1.5"),), reward_risks=(Decimal("2.0"),))
    tight = sweep(frame, entry_thresholds=(0.9,), atr_multipliers=(Decimal("1.5"),), reward_risks=(Decimal("2.0"),))

    assert tight[0].result.trade_count <= loose[0].result.trade_count

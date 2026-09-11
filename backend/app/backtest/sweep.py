"""参数扫描与稳健性报告。

**这里找的不是最优参数，而是「这套参数稳不稳」。** 41 天的 1h 只有几十笔交易，
挑出来的「最优」几乎一定是拟合了噪声。有意义的是看相邻取值之间的差距：如果
阈值从 0.30 挪到 0.35 结果就翻脸，那说明这个边际本来就是假的。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from typing import Any

from app.backtest.engine import BacktestResult, run_backtest
from app.domain.schemas import Candle
from app.signals.params import StrategyParams


@dataclass(frozen=True)
class SweepPoint:
    params: StrategyParams
    result: BacktestResult


def sweep(
    candles_by_timeframe: dict[str, list[Candle]],
    *,
    entry_thresholds: Sequence[float],
    atr_multipliers: Sequence[Decimal],
    reward_risks: Sequence[Decimal],
    base: StrategyParams | None = None,
    initial_equity: Decimal = Decimal(10_000),
    fee_pct: Decimal = Decimal("0.0008"),
) -> list[SweepPoint]:
    """对三个关键参数做笛卡尔积扫描，按平均 R 从高到低返回。

    只扫这三个：参数越多越容易过拟合，剩下的用 `base` 里的默认值锁死。
    """

    template = base or StrategyParams()
    points: list[SweepPoint] = []
    for threshold, atr_multiplier, reward_risk in product(
        entry_thresholds, atr_multipliers, reward_risks
    ):
        params = StrategyParams(
            **{
                **template.__dict__,
                "entry_threshold": threshold,
                "atr_multiplier": atr_multiplier,
                "reward_risk": reward_risk,
            }
        )
        points.append(
            SweepPoint(
                params=params,
                result=run_backtest(
                    candles_by_timeframe,
                    params=params,
                    initial_equity=initial_equity,
                    fee_pct=fee_pct,
                ),
            )
        )
    return sorted(points, key=lambda point: point.result.avg_r, reverse=True)


def robustness(points: Sequence[SweepPoint]) -> dict[str, dict[str, Any]]:
    """按轴汇总平均 R 的分布。

    极差（spread）就是稳健性指标：小 = 结论不依赖某个具体取值，大 = 大概率是拟合。
    """

    axes: dict[str, dict[Any, list[Decimal]]] = {
        "entry_threshold": {},
        "atr_multiplier": {},
        "reward_risk": {},
    }
    for point in points:
        axes["entry_threshold"].setdefault(point.params.entry_threshold, []).append(point.result.avg_r)
        axes["atr_multiplier"].setdefault(point.params.atr_multiplier, []).append(point.result.avg_r)
        axes["reward_risk"].setdefault(point.params.reward_risk, []).append(point.result.avg_r)

    report: dict[str, dict[str, Any]] = {}
    for axis, buckets in axes.items():
        if not buckets:
            continue
        # 每个取值先在自己的组合里取中位数，再看各取值之间的极差。
        medians = {
            value: sorted(values)[len(values) // 2]
            for value, values in buckets.items()
            if values
        }
        if not medians:
            continue
        low, high = min(medians.values()), max(medians.values())
        report[axis] = {
            "values": sorted(medians, key=lambda item: str(item)),
            "medians": medians,
            "min": low,
            "max": high,
            "spread": high - low,
        }
    return report

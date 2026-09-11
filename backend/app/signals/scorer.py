"""确定性规则打分卡：方向由两个周期的趋势一致性、动量、量能决定。

这是决策链的「方向来源」，取代了原来让 LLM 从散文里猜方向的做法。
分数可回测、可审计 —— 同一组指标永远得到同一个方向。

读哪两个周期由 `StrategyParams.entry_timeframe` / `trend_timeframe` 决定，
默认 1h/4h。拉到 12h/1d 时止损距离占比大得多，而手续费/R 与止损距离成反比 ——
这是把成本压下去的主要手段。
"""

from typing import Any

from app.signals.params import StrategyParams

HOLD = "HOLD"
LONG = "LONG"
SHORT = "SHORT"


def _trend_score(indicators: dict[str, Any], active: StrategyParams) -> float:
    """两个周期的趋势一致性：同向才给分，冲突归零。"""

    entry = indicators.get(active.entry_timeframe) or {}
    trend = indicators.get(active.trend_timeframe) or {}
    t_entry = str(entry.get("trend", "NEUTRAL")).upper()
    t_trend = str(trend.get("trend", "NEUTRAL")).upper()
    if t_entry == t_trend and t_entry == "BULLISH":
        return 1.0
    if t_entry == t_trend and t_entry == "BEARISH":
        return -1.0
    return 0.0


def _momentum_score(indicators: dict[str, Any], active: StrategyParams) -> float:
    """入场周期 RSI 偏离 50 的强度。"""

    entry = indicators.get(active.entry_timeframe) or {}
    rsi = entry.get("rsi_14")
    if rsi is None:
        return 0.0
    # RSI 60 → +0.4；RSI 40 → -0.4；接近 50 趋零
    return max(-1.0, min(1.0, (float(rsi) - 50.0) / 25.0))


def _volume_score(indicators: dict[str, Any], active: StrategyParams) -> float:
    """量能方向：两个周期的量变同号才给分。"""

    entry = indicators.get(active.entry_timeframe) or {}
    trend = indicators.get(active.trend_timeframe) or {}
    vol_entry = entry.get("volume_change_1")
    vol_trend = trend.get("volume_change_1")
    if vol_entry is None or vol_trend is None:
        return 0.0
    if float(vol_entry) > 0 and float(vol_trend) > 0:
        return 1.0
    if float(vol_entry) < 0 and float(vol_trend) < 0:
        return -1.0
    return 0.0


def score_signal(
    indicators: dict[str, Any],
    volatility_percentile: float | None = None,
    *,
    params: StrategyParams | None = None,
) -> tuple[str, float]:
    """返回 (方向, composite)。composite ∈ [-1, 1]，落库审计用。

    volatility_percentile 为 None 时跳过门控 —— 波动率分位算不出时不过度保守，
    也不把数据缺失当否决理由（spec 2.1：缺项归零，不整体 HOLD）。
    """

    active = params or StrategyParams()
    if volatility_percentile is not None and not (
        active.volatility_p20 <= float(volatility_percentile) <= active.volatility_p90
    ):
        return HOLD, 0.0

    if not indicators.get(active.entry_timeframe) or not indicators.get(active.trend_timeframe):
        # 缺了配置的周期就无从判断趋势 —— 不拿别的周期凑合，那等于没换周期。
        return HOLD, 0.0

    composite = (
        _trend_score(indicators, active) * active.trend_weight
        + _momentum_score(indicators, active) * active.momentum_weight
        + _volume_score(indicators, active) * active.volume_weight
    )
    if composite >= active.entry_threshold:
        return LONG, composite
    if composite <= -active.entry_threshold:
        return SHORT, composite
    return HOLD, composite


def score_direction(
    indicators: dict[str, Any],
    volatility_percentile: float | None = None,
    *,
    params: StrategyParams | None = None,
) -> str:
    """给方向打分（薄封装，供只关心方向的调用方）。"""

    direction, _ = score_signal(indicators, volatility_percentile, params=params)
    return direction

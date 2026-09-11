"""确定性规则打分卡：方向由 1h/4h 趋势一致性、动量、量能决定。

这是决策链的「方向来源」，取代了原来让 LLM 从散文里猜方向的做法。
分数可回测、可审计 —— 同一组指标永远得到同一个方向。
"""

from typing import Any

HOLD = "HOLD"
LONG = "LONG"
SHORT = "SHORT"

#: 权重，见 spec 2.1。41 天样本撑不起 7 个权重，第一版只上趋势/动量/量能。
TREND_WEIGHT = 0.40
MOMENTUM_WEIGHT = 0.25
VOLUME_WEIGHT = 0.15

#: 波动率门控分位区间：落在之外直接 HOLD。
VOLATILITY_P20 = 0.20
VOLATILITY_P90 = 0.90

#: composite 阈值：超过才开仓。显式常数，可回测调参。
ENTRY_THRESHOLD = 0.35


def _trend_score(indicators: dict[str, Any]) -> float:
    """1h/4h 趋势一致性：同向才给分，冲突归零。"""

    one_h = indicators.get("1h") or {}
    four_h = indicators.get("4h") or {}
    t1 = str(one_h.get("trend", "NEUTRAL")).upper()
    t4 = str(four_h.get("trend", "NEUTRAL")).upper()
    if t1 == t4 and t1 == "BULLISH":
        return 1.0
    if t1 == t4 and t1 == "BEARISH":
        return -1.0
    return 0.0


def _momentum_score(indicators: dict[str, Any]) -> float:
    """RSI 偏离 50 的强度，用 1h 的 RSI。"""

    one_h = indicators.get("1h") or {}
    rsi = one_h.get("rsi_14")
    if rsi is None:
        return 0.0
    # RSI 60 → +0.4；RSI 40 → -0.4；接近 50 趋零
    return max(-1.0, min(1.0, (float(rsi) - 50.0) / 25.0))


def _volume_score(indicators: dict[str, Any]) -> float:
    """量能方向：1h/4h 量变同号且与趋势同向才加分。"""

    one_h = indicators.get("1h") or {}
    four_h = indicators.get("4h") or {}
    vol_1h = one_h.get("volume_change_1")
    vol_4h = four_h.get("volume_change_1")
    if vol_1h is None or vol_4h is None:
        return 0.0
    if float(vol_1h) > 0 and float(vol_4h) > 0:
        return 1.0
    if float(vol_1h) < 0 and float(vol_4h) < 0:
        return -1.0
    return 0.0


def score_signal(
    indicators: dict[str, Any],
    volatility_percentile: float | None = None,
) -> tuple[str, float]:
    """返回 (方向, composite)。composite ∈ [-1, 1]，落库审计用。

    volatility_percentile 为 None 时跳过门控 —— 波动率分位算不出时不过度保守，
    也不把数据缺失当否决理由（spec 2.1：缺项归零，不整体 HOLD）。
    """

    if volatility_percentile is not None and not (
        VOLATILITY_P20 <= float(volatility_percentile) <= VOLATILITY_P90
    ):
        return HOLD, 0.0

    one_h = indicators.get("1h")
    four_h = indicators.get("4h")
    if not one_h or not four_h:
        # 没有 1h/4h 就无法判断趋势，不能只靠 5m 开仓。
        return HOLD, 0.0

    composite = (
        _trend_score(indicators) * TREND_WEIGHT
        + _momentum_score(indicators) * MOMENTUM_WEIGHT
        + _volume_score(indicators) * VOLUME_WEIGHT
    )
    if composite >= ENTRY_THRESHOLD:
        return LONG, composite
    if composite <= -ENTRY_THRESHOLD:
        return SHORT, composite
    return HOLD, composite


def score_direction(
    indicators: dict[str, Any],
    volatility_percentile: float | None = None,
) -> str:
    """给方向打分（薄封装，供只关心方向的调用方）。"""

    direction, _ = score_signal(indicators, volatility_percentile)
    return direction

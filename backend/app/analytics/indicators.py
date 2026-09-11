import math
from collections.abc import Iterable, Mapping
from itertools import pairwise
from typing import Any

from app.domain.schemas import Candle

INDICATOR_VERSION = "technical-v1"

_TIMEFRAME_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def timeframe_ms(timeframe: str) -> int:
    """`12h` → 43_200_000。

    认不出的周期直接抛：静默返回 0 会让每根 bar 都满足「已收盘」，
    那正是这个换算要防的错。
    """

    unit = timeframe[-1:].lower()
    digits = timeframe[:-1]
    if unit not in _TIMEFRAME_MS or not digits.isdigit():
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return int(digits) * _TIMEFRAME_MS[unit]


def closed_candles(candles: Iterable[Candle], timeframe: str, now_ms: int) -> list[Candle]:
    """只留下在 `now_ms` 之前**已经收盘**的 bar。

    最后一根 K 线是正在形成的：收盘价、最高最低、成交量都还在变。拿它算指标会
    带来两个后果，2026-09-11 线上实测都发生了：

    - **量能恒为负**：`volume_change_1` 拿半根的量比上一根完整的量，结构性偏低。
      十个品种里九个拿满 -1，量能项固定扣掉 0.15；而趋势分封顶 +0.40，于是
      所有品种都停在 0.25 附近、够不到 0.35 的入场阈值。
    - **回测对不上**：回测的决策时刻是「bar i 收盘」，它能拿到的 bar i 是完整的。
      线上用未完成 bar，两边算的根本不是同一组数。
    """

    duration = timeframe_ms(timeframe)
    return [candle for candle in candles if candle.open_time + duration <= now_ms]


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for current in values[period:]:
        value = alpha * current + (1 - alpha) * value
    return value


def _sma(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def _stddev(values: list[float], period: int) -> float | None:
    window = values[-period:]
    if len(window) < period:
        return None
    mean = sum(window) / period
    return math.sqrt(sum((value - mean) ** 2 for value in window) / period)


def _rsi(closes: list[float], period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [current - previous for previous, current in pairwise(closes)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = ((period - 1) * average_gain + gain) / period
        average_loss = ((period - 1) * average_loss + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100 - (100 / (1 + average_gain / average_loss))


def _atr(candles: list[Candle], period: int) -> float | None:
    if len(candles) < period + 1:
        return None
    true_ranges: list[float] = []
    for previous, current in pairwise(candles):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = ((period - 1) * value + true_range) / period
    return value


def _realized_volatility(closes: list[float], period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    returns = [math.log(current / previous) for previous, current in pairwise(closes) if previous > 0 and current > 0]
    if len(returns) < period:
        return None
    window = returns[-period:]
    mean = sum(window) / period
    return math.sqrt(sum((value - mean) ** 2 for value in window) / period) * math.sqrt(period)


def _timeframe_indicators(candles: Iterable[Candle]) -> dict[str, Any]:
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    closes = [candle.close for candle in ordered]
    volumes = [candle.volume for candle in ordered]
    ema_12 = _ema(closes, 12)
    ema_26 = _ema(closes, 26)
    middle = _sma(closes, 20)
    deviation = _stddev(closes, 20)
    return {
        "indicator_version": INDICATOR_VERSION,
        "data_points": len(ordered),
        "last_close": closes[-1] if closes else None,
        "ema_12": ema_12,
        "ema_26": ema_26,
        "ema_spread_pct": ((ema_12 - ema_26) / ema_26) if ema_12 is not None and ema_26 else None,
        "rsi_14": _rsi(closes, 14),
        "atr_14": _atr(ordered, 14),
        "bollinger_middle_20": middle,
        "bollinger_upper_20": middle + 2 * deviation if middle is not None and deviation is not None else None,
        "bollinger_lower_20": middle - 2 * deviation if middle is not None and deviation is not None else None,
        "realized_volatility_20": _realized_volatility(closes, 20),
        "volume_change_1": ((volumes[-1] / volumes[-2]) - 1) if len(volumes) >= 2 and volumes[-2] else None,
        "trend": (
            "BULLISH"
            if ema_12 is not None and ema_26 is not None and ema_12 > ema_26
            else "BEARISH"
            if ema_12 is not None and ema_26 is not None and ema_12 < ema_26
            else "NEUTRAL"
        ),
    }


def calculate_indicators(
    candles_by_timeframe: Mapping[str, Iterable[Candle]],
    *,
    now_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    """按周期算指标。

    `now_ms` 给了就只用**已收盘**的 bar（见 `closed_candles`）—— 这是决策路径上
    的正确用法，交易周期与回测都该传。不传的只有纯计算场景（单测、离线分析）。

    参数刻意保留为可选而不是必填：回测里「决策时刻」不是当前时间，硬塞一个默认值
    只会让两边悄悄用上不同的时钟。
    """

    if now_ms is None:
        return {timeframe: _timeframe_indicators(candles) for timeframe, candles in candles_by_timeframe.items()}
    return {
        timeframe: _timeframe_indicators(closed_candles(candles, timeframe, now_ms))
        for timeframe, candles in candles_by_timeframe.items()
    }

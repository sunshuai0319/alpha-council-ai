import math
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

from app.domain.schemas import Candle

INDICATOR_VERSION = "technical-v1"


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


def calculate_indicators(candles_by_timeframe: dict[str, Iterable[Candle]]) -> dict[str, dict[str, Any]]:
    return {timeframe: _timeframe_indicators(candles) for timeframe, candles in candles_by_timeframe.items()}

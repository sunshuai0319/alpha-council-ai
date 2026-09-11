import pytest

from app.analytics.indicators import calculate_indicators, timeframe_ms
from app.domain.schemas import Candle

HALF_DAY_MS = 12 * 3_600_000


def _candles(count: int = 40) -> list[Candle]:
    return [
        Candle(
            symbol="BTC-USDT",
            timeframe="5m",
            open_time=index * 300_000,
            open=100 + index,
            high=102 + index,
            low=98 + index,
            close=100 + index,
            volume=100 + index * 2,
        )
        for index in range(count)
    ]


def test_calculate_indicators_returns_versioned_trend_and_volatility_metrics() -> None:
    result = calculate_indicators({"5m": _candles()})

    metrics = result["5m"]
    assert metrics["indicator_version"] == "technical-v1"
    assert metrics["ema_12"] > metrics["ema_26"]
    assert metrics["rsi_14"] == 100.0
    assert metrics["atr_14"] > 0
    assert metrics["bollinger_upper_20"] > metrics["bollinger_middle_20"]
    assert metrics["realized_volatility_20"] >= 0
    assert metrics["volume_change_1"] > 0


def test_calculate_indicators_reports_insufficient_history_without_fabricating_values() -> None:
    result = calculate_indicators({"5m": _candles(5)})

    assert result["5m"]["indicator_version"] == "technical-v1"
    assert result["5m"]["ema_12"] is None
    assert result["5m"]["rsi_14"] is None
    assert result["5m"]["atr_14"] is None
    assert result["5m"]["data_points"] == 5


def _half_day_candles(count: int = 30, *, forming_volume: float = 1) -> list[Candle]:
    """30 根完整的 12h bar，外加一根刚开始、量还很小的 bar。"""

    candles = [
        Candle(
            symbol="BTC-USDT",
            timeframe="12h",
            open_time=index * HALF_DAY_MS,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1000 + index,
        )
        for index in range(count)
    ]
    candles.append(
        Candle(
            symbol="BTC-USDT",
            timeframe="12h",
            open_time=count * HALF_DAY_MS,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=forming_volume,
        )
    )
    return candles


def test_indicators_exclude_the_still_forming_bar() -> None:
    """最后一根 K 线还没收盘，拿它比上一根完整的量，必然「缩量」。

    这正是线上全 HOLD 的元凶：`volume_change_1` 被结构性压低 → 量能分恒为 -1 →
    固定扣掉 0.15，而趋势分封顶 +0.40，于是所有品种都卡在 0.35 阈值下方。
    """

    candles = _half_day_candles()
    one_hour_in = candles[-1].open_time + 3_600_000

    closed = calculate_indicators({"12h": candles}, now_ms=one_hour_in)["12h"]
    naive = calculate_indicators({"12h": candles})["12h"]

    assert closed["data_points"] == len(candles) - 1
    assert closed["volume_change_1"] > 0, "完整对完整，量在涨"
    assert naive["volume_change_1"] < 0, "半根对整根，假缩量"


def test_a_bar_is_only_closed_once_its_duration_has_elapsed() -> None:
    candles = _half_day_candles()
    last = candles[-1]

    assert calculate_indicators({"12h": candles}, now_ms=last.open_time)["12h"]["data_points"] == len(candles) - 1
    assert calculate_indicators({"12h": candles}, now_ms=last.open_time + HALF_DAY_MS)["12h"]["data_points"] == len(candles)


def test_unknown_timeframes_are_rejected_rather_than_treated_as_zero() -> None:
    """静默当成 0 会让每根 bar 都被当成已收盘 —— 那恰恰是这个函数要防的错。"""

    assert timeframe_ms("12h") == HALF_DAY_MS
    assert timeframe_ms("5m") == 300_000
    assert timeframe_ms("1d") == 86_400_000
    with pytest.raises(ValueError):
        timeframe_ms("12x")

from app.analytics.indicators import calculate_indicators
from app.domain.schemas import Candle


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

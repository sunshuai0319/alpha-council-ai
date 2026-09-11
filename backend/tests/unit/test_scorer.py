"""打分卡：方向由 1h/4h 趋势一致性、动量、量能决定；波动率是门控。"""


from app.signals.scorer import HOLD, LONG, SHORT, score_direction


def _tf(*, trend: str, rsi: float | None, volume: float | None = 0.1, ema: float | None = 0.001) -> dict:
    return {
        "trend": trend,
        "rsi_14": rsi,
        "volume_change_1": volume,
        "ema_spread_pct": ema,
        "atr_14": 2.0,
    }


def _indicators(*, volume_1h: float | None = 0.1, volume_4h: float | None = 0.1) -> dict:
    """对齐的多头结构，测试按需覆盖。"""
    return {
        "1h": _tf(trend="BULLISH", rsi=55.0, volume=volume_1h),
        "4h": _tf(trend="BULLISH", rsi=58.0, volume=volume_4h),
        "5m": _tf(trend="BULLISH", rsi=60.0),
    }


def test_aligned_bullish_trends_with_volume_confirmation_long() -> None:
    assert score_direction(_indicators()) == LONG


def test_aligned_bearish_trends_short() -> None:
    ind = {
        "1h": _tf(trend="BEARISH", rsi=45.0, volume=-0.1),
        "4h": _tf(trend="BEARISH", rsi=42.0, volume=-0.1),
        "5m": _tf(trend="BEARISH", rsi=40.0),
    }
    assert score_direction(ind) == SHORT


def test_conflicting_timeframes_hold() -> None:
    ind = {
        "1h": _tf(trend="BULLISH", rsi=55.0),
        "4h": _tf(trend="BEARISH", rsi=42.0),
        "5m": _tf(trend="NEUTRAL", rsi=50.0),
    }
    assert score_direction(ind) == HOLD


def test_high_volatility_gate_holds() -> None:
    """波动率分位 > p90 → 直接 HOLD（门控，不看方向）。"""
    ind = _indicators()
    assert score_direction(ind, volatility_percentile=0.95) == HOLD
    assert score_direction(ind, volatility_percentile=0.5) == LONG


def test_low_volatility_gate_holds() -> None:
    ind = _indicators()
    assert score_direction(ind, volatility_percentile=0.1) == HOLD


def test_volume_against_the_trend_pulls_back() -> None:
    """方向多但量能在缩（volume_change_1 为负）→ 削弱多头，可能 HOLD。"""
    ind = _indicators(volume_1h=-0.5, volume_4h=-0.5)
    # 方向 +1 但量能 -1，趋势权重撑不住 0.35 阈值
    assert score_direction(ind) == HOLD


def test_volatility_percentile_none_skips_the_gate() -> None:
    """波动率数据缺失时不过度保守：门控只在有分位时才生效。"""
    assert score_direction(_indicators()) == LONG


def test_missing_timeframes_hold() -> None:
    """1h 或 4h 缺失 → 无法判断趋势，HOLD（不能只靠 5m 开仓）。"""
    assert score_direction({"5m": _tf(trend="BULLISH", rsi=60.0)}) == HOLD


def test_scorer_reads_the_configured_timeframes() -> None:
    """同一个 indicators，换个周期键名就该用另一组数据判断。

    拉到日线时不能还去读 1h/4h —— 那样等于没换周期。
    """
    from app.signals.params import StrategyParams

    indicators = {
        "12h": _tf(trend="BULLISH", rsi=58.0),
        "1d": _tf(trend="BULLISH", rsi=60.0),
        # 故意放一组反向的小时级数据：若仍读 1h/4h，结论会反过来
        "1h": _tf(trend="BEARISH", rsi=40.0),
        "4h": _tf(trend="BEARISH", rsi=38.0),
    }
    daily = StrategyParams(entry_timeframe="12h", trend_timeframe="1d")

    assert score_direction(indicators, params=daily) == LONG
    assert score_direction(indicators) == SHORT  # 默认仍读 1h/4h


def test_scorer_holds_when_the_configured_timeframes_are_absent() -> None:
    from app.signals.params import StrategyParams

    indicators = {"1h": _tf(trend="BULLISH", rsi=60.0)}
    daily = StrategyParams(entry_timeframe="12h", trend_timeframe="1d")

    assert score_direction(indicators, params=daily) == HOLD

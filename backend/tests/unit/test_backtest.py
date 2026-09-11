"""回测引擎：用与线上同一套信号/仓位/持仓管理代码跑历史 K 线。

定位是**筛子不是优化器** —— 41 天 1h 只有几十笔交易，能看到的是「会不会明显亏」
「对参数是不是极度敏感」，不是「最优参数是多少」。
"""

from decimal import Decimal

import pytest

from app.backtest.engine import _intrabar_exit, _OpenPosition, _settle, run_backtest
from app.domain.schemas import Candle
from app.signals.params import StrategyParams

HOUR_MS = 3_600_000


def _candles(closes: list[float], *, pad: float = 0.5) -> list[Candle]:
    """把一串收盘价造成 1h K 线；open 取前一根收盘，high/low 在实体上下留 pad。"""

    out: list[Candle] = []
    for index, close in enumerate(closes):
        open_ = closes[index - 1] if index else close
        out.append(
            Candle(
                symbol="BTC-USDT",
                timeframe="1h",
                open_time=1_700_000_000_000 + index * HOUR_MS,
                open=open_,
                high=max(open_, close) + pad,
                low=min(open_, close) - pad,
                close=close,
                volume=1000.0,
            )
        )
    return out


def _resample_4h(candles_1h: list[Candle]) -> list[Candle]:
    """4 根 1h 合成一根 4h —— 两个周期必须自洽，否则指标互相矛盾。"""

    out: list[Candle] = []
    for index in range(0, len(candles_1h) - 3, 4):
        chunk = candles_1h[index : index + 4]
        out.append(
            Candle(
                symbol="BTC-USDT",
                timeframe="4h",
                open_time=chunk[0].open_time,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
            )
        )
    return out


def _uptrend(length: int, *, start: float = 100.0, step: float = 0.15) -> list[float]:
    return [start + index * step for index in range(length)]


def _frame(closes: list[float]) -> dict[str, list[Candle]]:
    one_h = _candles(closes)
    return {"1h": one_h, "4h": _resample_4h(one_h)}


def test_no_signal_means_no_trades() -> None:
    """横盘不产生信号时，一笔都不该开。"""
    flat = [100.0 + (0.01 if index % 2 else -0.01) for index in range(200)]
    result = run_backtest(_frame(flat))

    assert result.trade_count == 0
    assert result.total_return_pct == pytest.approx(0.0)


def test_uptrend_produces_a_long_that_reaches_the_target() -> None:
    """强上升趋势 → 出多头信号 → 价格继续涨到止盈。"""
    closes = _uptrend(160) + [124.0 + index * 1.2 for index in range(40)]
    result = run_backtest(_frame(closes))

    assert result.trade_count >= 1
    first = result.trades[0]
    assert first.side == "LONG"
    assert first.exit_reason in {"take_profit", "structure_invalidated", "time_stop"}
    assert result.total_return_pct > 0


def _position(**overrides) -> _OpenPosition:
    base = {
        "side": "LONG",
        "entry_time": 0,
        "entry_price": Decimal(100),
        "quantity": Decimal(1),
        "initial_stop": Decimal(97),
        "effective_stop": Decimal(97),
        "take_profit": Decimal(106),
        "peak_price": Decimal(100),
    }
    return _OpenPosition(**{**base, **overrides})


def test_a_stop_at_the_original_level_costs_one_r() -> None:
    """初始止损被打掉 = -1R（不计手续费）。R 的定义就在这。"""
    trade = _settle(
        _position(), exit_time=1, exit_price=Decimal(97), exit_reason="stop", fee_pct=Decimal(0)
    )
    assert trade.r_multiple == Decimal(-1)


def test_a_crash_after_entry_produces_a_stop_exit() -> None:
    """入场后立刻反向 → 止损离场。"""
    closes = _uptrend(160) + [118.0 - index * 1.5 for index in range(1, 41)]
    result = run_backtest(_frame(closes))

    assert "stop" in result.exit_reason_counts, result.exit_reason_counts


def test_a_trailed_stop_can_still_exit_profitably() -> None:
    """止损被推到入场价之上后再触发，是**盈利**离场 —— 这正是移动止损的目的。

    所以「exit_reason == stop」不等于亏损，别把两者划等号。
    """
    position = _position(effective_stop=Decimal(102), peak_price=Decimal(105))
    trade = _settle(
        position, exit_time=1, exit_price=Decimal(102), exit_reason="stop", fee_pct=Decimal(0)
    )
    assert trade.r_multiple > 0


def test_a_bar_touching_both_stop_and_target_counts_as_the_stop() -> None:
    """同一根 K 线内止损止盈都被触及时按止损算 —— 保守，不给回测送便宜。

    直接测规则本身：K 线内部路径不可知，从宽处理会让回测凭空多赚。
    """
    position = _position(take_profit=Decimal(106))
    both = _candles([100.0])[0].model_copy(update={"high": 999.0, "low": 1.0})

    assert _intrabar_exit(position, both) == (Decimal(97), "stop")


def test_a_short_touches_its_stop_on_the_way_up() -> None:
    position = _position(side="SHORT", initial_stop=Decimal(103), effective_stop=Decimal(103),
                         take_profit=Decimal(94))
    bar = _candles([100.0])[0].model_copy(update={"high": 200.0, "low": 50.0})

    assert _intrabar_exit(position, bar) == (Decimal(103), "stop")


def test_fees_reduce_the_result_and_are_charged_on_both_legs() -> None:
    """手续费按每边 0.08% 收 —— 虚拟盘实测值，不收就高估收益。"""
    closes = _uptrend(160) + [124.0 + index * 1.2 for index in range(40)]
    frame = _frame(closes)

    free = run_backtest(frame, fee_pct=Decimal(0))
    charged = run_backtest(frame, fee_pct=Decimal("0.0008"))

    assert charged.trade_count == free.trade_count
    if free.trade_count:
        assert charged.final_equity < free.final_equity


def test_only_one_position_at_a_time() -> None:
    """与线上一致：单品种一仓，不会重叠。"""
    closes = _uptrend(400)
    result = run_backtest(_frame(closes))

    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert earlier.exit_time <= later.entry_time


def test_result_reports_the_metrics_needed_to_judge_an_edge() -> None:
    closes = _uptrend(160) + [118.0 - index * 1.5 for index in range(1, 41)]
    result = run_backtest(_frame(closes))

    assert 0.0 <= result.win_rate <= 1.0
    assert result.max_drawdown_pct >= 0.0
    assert isinstance(result.exit_reason_counts, dict)
    assert result.avg_r == pytest.approx(
        sum(t.r_multiple for t in result.trades) / len(result.trades), abs=Decimal("1e-6")
    )


def test_params_can_be_swept_per_run() -> None:
    """阈值高到不可能触发 → 不该有任何交易。"""
    closes = _uptrend(160) + [124.0 + index * 1.2 for index in range(40)]
    frame = _frame(closes)

    assert run_backtest(frame).trade_count >= 1
    assert run_backtest(frame, params=StrategyParams(entry_threshold=0.99)).trade_count == 0


def test_short_series_is_skipped_not_crashed() -> None:
    """K 线不足以算指标时返回空结果，不抛异常。"""
    result = run_backtest(_frame([100.0, 101.0, 102.0]))
    assert result.trade_count == 0

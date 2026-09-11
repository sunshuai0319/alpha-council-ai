"""回测引擎：用**与线上同一套**信号 / 仓位 / 持仓管理代码跑历史 K 线。

刻意复用 `score_signal` / `size_position` / `manage`，而不是重写一遍逻辑 ——
重写的回测只能证明「那套重写的代码能赚钱」，与线上跑的东西无关。

**定位是筛子，不是优化器。** 41 天的 1h 只有几十笔交易，统计意义很弱：能回答
「会不会明显亏」「对参数是不是极度敏感」，不能回答「最优参数是多少」。

**能力边界**：`depth` / `trades` / `fundingRate` / `openInterest` 没有历史，
所以回测验证不了微观结构相关的部分（structure veto agent 的一半输入），也
没有 LLM 否决与熔断 —— 覆盖的是「打分卡 + 仓位反推 + 持仓管理」这条主链。

模拟规则一律**偏保守**，宁可低估收益：

- 信号出现在 bar i 的收盘 → 以 **bar i+1 的开盘价**成交，不给「同根 K 线内成交」的便宜
- 用 bar 的 high/low 判定止损止盈；同一根都触及时**按止损算**
- 手续费每边照收（虚拟盘实测 taker 0.08%）
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.analytics.indicators import calculate_indicators
from app.domain.schemas import Candle
from app.positions.manager import CLOSE as POSITION_CLOSE
from app.positions.manager import manage as manage_position
from app.signals.params import StrategyParams
from app.signals.scorer import HOLD, LONG, SHORT, score_signal
from app.signals.sizing import size_position

#: 入场周期至少要这么多根，否则 EMA26 / RSI14 全是 None。
MIN_BARS = 40
#: 趋势周期至少要这么几根，否则 trend 字段全是 NEUTRAL。
MIN_TREND_BARS = 10

EXIT_STOP = "stop"
EXIT_TARGET = "take_profit"
EXIT_STRUCTURE = "structure_invalidated"
EXIT_TIME = "time_stop"


def _as_datetime(open_time_ms: int) -> datetime:
    return datetime.fromtimestamp(open_time_ms / 1000, tz=UTC)


@dataclass(frozen=True)
class BacktestTrade:
    side: str
    entry_time: int
    entry_price: Decimal
    quantity: Decimal
    initial_stop: Decimal
    take_profit: Decimal
    exit_time: int
    exit_price: Decimal
    exit_reason: str
    r_multiple: Decimal
    pnl: Decimal


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[BacktestTrade, ...] = ()
    initial_equity: Decimal = Decimal(0)
    final_equity: Decimal = Decimal(0)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for trade in self.trades if trade.pnl > 0) / len(self.trades)

    @property
    def avg_r(self) -> Decimal:
        """每笔的平均 R —— 这才是判断「这套参数值不值得跑」的量级。"""

        if not self.trades:
            return Decimal(0)
        return sum((trade.r_multiple for trade in self.trades), Decimal(0)) / len(self.trades)

    @property
    def total_return_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return float((self.final_equity - self.initial_equity) / self.initial_equity) * 100

    @property
    def max_drawdown_pct(self) -> float:
        equity = self.initial_equity
        peak = equity
        worst = Decimal(0)
        for trade in self.trades:
            equity += trade.pnl
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak)
        return float(worst) * 100

    @property
    def exit_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trade in self.trades:
            counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
        return counts


@dataclass
class _OpenPosition:
    side: str
    entry_time: int
    entry_price: Decimal
    quantity: Decimal
    initial_stop: Decimal
    effective_stop: Decimal
    take_profit: Decimal
    peak_price: Decimal


def _indicators_at(
    candles_by_timeframe: dict[str, list[Candle]],
    entry_timeframe: str,
    index: int,
) -> dict[str, Any]:
    """只用截至 index 的数据 —— 用未来 K 线是回测最常见的前视偏差。

    只算打分卡真正会读的两个周期，不做无用的全量计算。
    """

    entry = candles_by_timeframe[entry_timeframe]
    cutoff = entry[index].open_time
    return calculate_indicators(
        {
            timeframe: [candle for candle in candles if candle.open_time <= cutoff]
            for timeframe, candles in candles_by_timeframe.items()
        }
    )


def _intrabar_exit(position: _OpenPosition, bar: Candle) -> tuple[Decimal, str] | None:
    """用 bar 的极值判断止损/止盈。两个都触及时**按止损算**（保守）。"""

    long = position.side == "LONG"
    stop_price = float(position.effective_stop)
    target_price = float(position.take_profit)
    stop_hit = bar.low <= stop_price if long else bar.high >= stop_price
    target_hit = bar.high >= target_price if long else bar.low <= target_price
    if stop_hit:
        return position.effective_stop, EXIT_STOP
    if target_hit:
        return position.take_profit, EXIT_TARGET
    return None


def _settle(
    position: _OpenPosition,
    *,
    exit_time: int,
    exit_price: Decimal,
    exit_reason: str,
    fee_pct: Decimal,
) -> BacktestTrade:
    """按两个方向各收一次手续费，再算这笔的盈亏与 R。"""

    direction = Decimal(1) if position.side == "LONG" else Decimal(-1)
    gross = (exit_price - position.entry_price) * position.quantity * direction
    fee = (position.entry_price + exit_price) * position.quantity * fee_pct
    pnl = gross - fee
    risk = abs(position.entry_price - position.initial_stop) * position.quantity
    return BacktestTrade(
        side=position.side,
        entry_time=position.entry_time,
        entry_price=position.entry_price,
        quantity=position.quantity,
        initial_stop=position.initial_stop,
        take_profit=position.take_profit,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=exit_reason,
        r_multiple=pnl / risk if risk > 0 else Decimal(0),
        pnl=pnl,
    )


def run_backtest(
    candles_by_timeframe: dict[str, list[Candle]],
    *,
    params: StrategyParams | None = None,
    initial_equity: Decimal = Decimal(10_000),
    fee_pct: Decimal = Decimal("0.0008"),
) -> BacktestResult:
    """跑一遍历史，返回逐笔交易与汇总指标。"""

    active = params or StrategyParams()
    # 按 open_time 排序：WEEX 的 klines 不保证有序（实测），不排会毁掉一切。
    frame = {
        timeframe: sorted(candles, key=lambda candle: candle.open_time)
        for timeframe, candles in candles_by_timeframe.items()
    }
    entry_timeframe = active.entry_timeframe
    bars = frame.get(entry_timeframe, [])
    trend_bars = frame.get(active.trend_timeframe, [])
    if len(bars) < MIN_BARS + 2 or len(trend_bars) < MIN_TREND_BARS:
        return BacktestResult(initial_equity=initial_equity, final_equity=initial_equity)

    trades: list[BacktestTrade] = []
    equity = initial_equity
    position: _OpenPosition | None = None

    for index in range(MIN_BARS, len(bars) - 1):
        bar = bars[index]

        if position is not None:
            intrabar = _intrabar_exit(position, bar)
            if intrabar is not None:
                exit_price, reason = intrabar
                trade = _settle(
                    position,
                    exit_time=bar.open_time,
                    exit_price=exit_price,
                    exit_reason=reason,
                    fee_pct=fee_pct,
                )
                trades.append(trade)
                equity += trade.pnl
                position = None
            else:
                indicators = _indicators_at(frame, entry_timeframe, index)
                _, composite = score_signal(indicators, params=active)
                atr = (indicators.get(entry_timeframe) or {}).get("atr_14")
                decision = manage_position(
                    side=position.side,
                    entry_price=position.entry_price,
                    initial_stop=position.initial_stop,
                    effective_stop=position.effective_stop,
                    peak_price=position.peak_price,
                    price=Decimal(str(bar.close)),
                    atr=Decimal(str(atr)) if atr else None,
                    # 时间止损在回测里同样生效 —— 用 K 线时刻而不是跳过它。
                    opened_at=_as_datetime(position.entry_time),
                    now=_as_datetime(bar.open_time),
                    signal_score=composite,
                    params=active,
                )
                if decision.action is POSITION_CLOSE:
                    trade = _settle(
                        position,
                        exit_time=bar.open_time,
                        exit_price=Decimal(str(bar.close)),
                        exit_reason=(
                            EXIT_STRUCTURE if decision.reason == "structure_invalidated" else EXIT_TIME
                        ),
                        fee_pct=fee_pct,
                    )
                    trades.append(trade)
                    equity += trade.pnl
                    position = None
                else:
                    position.effective_stop = decision.effective_stop
                    position.peak_price = decision.peak_price

        # 单品种一仓：手里有仓就不再看新信号。
        if position is not None:
            continue

        indicators = _indicators_at(frame, entry_timeframe, index)
        direction, _ = score_signal(indicators, params=active)
        if direction == HOLD:
            continue
        atr = (indicators.get(entry_timeframe) or {}).get("atr_14")
        if atr is None or atr <= 0:
            continue
        next_bar = bars[index + 1]
        entry_price = Decimal(str(next_bar.open))
        if entry_price <= 0:
            continue
        plan = size_position(
            equity=equity,
            entry=entry_price,
            atr=Decimal(str(atr)),
            side=direction,
            params=active,
        )
        quantity = plan.notional / entry_price
        if quantity <= 0:
            continue
        position = _OpenPosition(
            side=LONG if direction == LONG else SHORT,
            entry_time=next_bar.open_time,
            entry_price=entry_price,
            quantity=quantity,
            initial_stop=plan.stop_loss,
            effective_stop=plan.stop_loss,
            take_profit=plan.take_profit,
            peak_price=entry_price,
        )

    return BacktestResult(
        trades=tuple(trades),
        initial_equity=initial_equity,
        final_equity=equity,
    )

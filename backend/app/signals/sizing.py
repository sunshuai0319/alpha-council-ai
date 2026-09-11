"""仓位与 SL/TP 由风险预算反推，不由 LLM 拍。

这绕开了老问题：LLM 填 SL 时不知道 max_single_trade_risk_pct 反推出的隐含上限
（0.5% ÷ 20% = 2.5%），蒙错就被 RiskEngine 的 single_trade_risk 拒。规则化的好处
是无论 LLM 说什么，仓位永远满足风控 —— RiskEngine 仍是最终硬边界，这里只是让它
更可能一次通过。
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

#: 单笔风险占权益的比例上限，与 RiskLimits.max_single_trade_risk_pct 对齐。
DEFAULT_RISK_PCT = Decimal("0.005")
#: 名义敞口占权益的上限，与 RiskLimits.max_position_notional_pct 对齐。
DEFAULT_MAX_NOTIONAL_PCT = Decimal("0.20")
#: 止损 = k × ATR(1h)，k 默认 1.5。
DEFAULT_ATR_MULTIPLIER = Decimal("1.5")
#: TP = entry ± rr × stop_distance。
DEFAULT_RR = Decimal("2.0")
#: 灾难止损 = disaster_multiplier × stop_distance（唯一挂交易所侧的宽止损）。
DEFAULT_DISASTER_MULTIPLIER = Decimal("3.0")


@dataclass(frozen=True)
class PositionPlan:
    side: str
    stop_distance: Decimal
    position_size_pct: Decimal
    notional: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    disaster_stop: Decimal


def size_position(
    *,
    equity: Decimal,
    entry: Decimal,
    atr: Decimal,
    side: str,
    risk_pct: Decimal = DEFAULT_RISK_PCT,
    max_notional_pct: Decimal = DEFAULT_MAX_NOTIONAL_PCT,
    atr_multiplier: Decimal = DEFAULT_ATR_MULTIPLIER,
    rr: Decimal = DEFAULT_RR,
    disaster_multiplier: Decimal = DEFAULT_DISASTER_MULTIPLIER,
) -> PositionPlan:
    """按风险预算反推仓位与止损止盈。

    stop_distance 来自 ATR 而不是用户手填，所以 RiskEngine 的
    single_trade_risk（单笔风险 ≤ 0.5%）必然通过 —— 数学上保证，除非上游给了
    非正的 equity / entry / atr。
    """

    stop_distance = atr * atr_multiplier
    if stop_distance <= 0 or entry <= 0 or equity <= 0:
        raise ValueError("equity, entry and atr must be positive")

    risk_budget = equity * risk_pct
    stop_pct = stop_distance / entry
    notional_raw = risk_budget / stop_pct
    notional = min(notional_raw, equity * max_notional_pct)
    position_size_pct = notional / equity

    if side == "SHORT":
        stop_loss = entry + stop_distance
        take_profit = entry - stop_distance * rr
        disaster_stop = entry + stop_distance * disaster_multiplier
    else:
        stop_loss = entry - stop_distance
        take_profit = entry + stop_distance * rr
        disaster_stop = entry - stop_distance * disaster_multiplier

    # 精度向下收敛（6 位小数，行情量级足够），避免浮点残留。
    quant = Decimal("0.000001")
    return PositionPlan(
        side=side,
        stop_distance=stop_distance.quantize(quant, rounding=ROUND_DOWN),
        position_size_pct=position_size_pct.quantize(quant, rounding=ROUND_DOWN),
        notional=notional.quantize(quant, rounding=ROUND_DOWN),
        stop_loss=stop_loss.quantize(quant, rounding=ROUND_DOWN),
        take_profit=take_profit.quantize(quant, rounding=ROUND_DOWN),
        disaster_stop=disaster_stop.quantize(quant, rounding=ROUND_DOWN),
    )

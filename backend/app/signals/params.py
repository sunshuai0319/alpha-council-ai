"""策略参数集中在一处，可以逐次覆盖。

**为什么要有这个**：这些数值原来散落在三个模块的模块级常量里，改一个要动代码、
要重部署 —— 回测器就没法扫参数，也没法做敏感性分析。

风险预算（`risk_pct` / `max_notional_pct`）不在这里另开一套字段：它们与
``RiskLimits`` 是同一个概念，共用 Settings 里的那两个值，避免出现两个真相来源。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型标注，避免 config ↔ signals 循环导入
    from app.config import Settings


@dataclass(frozen=True)
class StrategyParams:
    """打分卡 + 仓位反推 + 持仓管理的全部阈值。

    默认值就是系统一直在用的值 —— 改这个 dataclass 会改变线上行为，别随手动。
    """

    # ---- 打分卡 ----
    #: 打分卡读哪两个周期。默认 1h/4h 是系统一直在用的；拉到 12h/1d 时
    #: 止损距离占比会大得多，而手续费/R = 2×费率÷止损距离，成本压力随之下降。
    entry_timeframe: str = "1h"
    trend_timeframe: str = "4h"
    trend_weight: float = 0.40
    momentum_weight: float = 0.25
    volume_weight: float = 0.15
    #: composite 超过这个绝对值才开仓。
    entry_threshold: float = 0.35
    #: 波动率分位门控区间，落在之外直接 HOLD。
    volatility_p20: float = 0.20
    volatility_p90: float = 0.90

    # ---- 仓位与止损止盈 ----
    #: 单笔风险占权益的比例上限（与 RiskLimits.max_single_trade_risk_pct 同源）。
    risk_pct: Decimal = Decimal("0.005")
    #: 名义敞口占权益上限（与 RiskLimits.max_position_notional_pct 同源）。
    max_notional_pct: Decimal = Decimal("0.20")
    #: 止损 = k × ATR(1h)。
    atr_multiplier: Decimal = Decimal("1.5")
    #: 止盈 = entry ± rr × 止损距离。
    reward_risk: Decimal = Decimal("2.0")
    #: 交易所侧那条宽灾难止损 = k × 止损距离。
    disaster_multiplier: Decimal = Decimal("3.0")

    # ---- 持仓管理 ----
    #: 浮盈到多少 R 把止损移到保本。
    breakeven_r: Decimal = Decimal("1.0")
    #: 移动止损的跟随距离 = k × ATR(1h)。
    trail_atr_multiplier: Decimal = Decimal("1.0")
    #: 持仓超过这么久且浮盈不足 time_stop_min_r 就平掉。
    time_stop_hours: int = 48
    time_stop_min_r: Decimal = Decimal("0.3")

    @classmethod
    def from_settings(cls, settings: "Settings") -> "StrategyParams":
        """用 Settings 里的值覆盖默认值（env 可改，改参不用动代码）。"""

        return cls(
            entry_timeframe=settings.strategy_entry_timeframe,
            trend_timeframe=settings.strategy_trend_timeframe,
            trend_weight=settings.strategy_trend_weight,
            momentum_weight=settings.strategy_momentum_weight,
            volume_weight=settings.strategy_volume_weight,
            entry_threshold=settings.strategy_entry_threshold,
            atr_multiplier=Decimal(str(settings.strategy_atr_multiplier)),
            reward_risk=Decimal(str(settings.strategy_reward_risk)),
            disaster_multiplier=Decimal(str(settings.strategy_disaster_multiplier)),
            breakeven_r=Decimal(str(settings.strategy_breakeven_r)),
            trail_atr_multiplier=Decimal(str(settings.strategy_trail_atr_multiplier)),
            time_stop_hours=settings.strategy_time_stop_hours,
            time_stop_min_r=Decimal(str(settings.strategy_time_stop_min_r)),
            # 风险预算与 RiskLimits 同源，不另开字段。
            risk_pct=Decimal(str(settings.max_single_trade_risk_pct)),
            max_notional_pct=Decimal(str(settings.max_position_notional_pct)),
        )

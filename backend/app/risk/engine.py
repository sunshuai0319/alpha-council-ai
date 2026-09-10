from dataclasses import dataclass
from decimal import Decimal
from time import time
from typing import Any

from app.config import Settings, get_settings
from app.domain.enums import RiskStatus
from app.domain.schemas import RiskDecision


@dataclass(frozen=True)
class RiskLimits:
    max_leverage: int = 20
    max_position_notional_pct: Decimal = Decimal("0.20")
    max_single_trade_risk_pct: Decimal = Decimal("0.005")
    max_daily_loss_pct: Decimal = Decimal("0.05")
    max_consecutive_losses: int = 3
    max_daily_trades: int = 20
    market_data_max_age_seconds: int = 90

    @classmethod
    def from_settings(cls, settings: Settings) -> "RiskLimits":
        return cls(
            max_leverage=settings.max_leverage,
            max_position_notional_pct=Decimal(str(settings.max_position_notional_pct)),
            max_single_trade_risk_pct=Decimal(str(settings.max_single_trade_risk_pct)),
            max_daily_loss_pct=Decimal(str(settings.max_daily_loss_pct)),
            max_consecutive_losses=settings.max_consecutive_losses,
            max_daily_trades=settings.max_daily_trades,
            market_data_max_age_seconds=settings.market_data_max_age_seconds,
        )


def daily_loss_pct(
    *,
    day_start_equity: Decimal | float,
    current_equity: Decimal | float,
) -> Decimal:
    """当日回撤比例，用于 ``max_daily_loss_pct`` 熔断。

    以当天观测到的第一笔权益为基准；盈利或持平时为 0（熔断只看亏损）。
    基准非正时无法计算比例，返回 0 —— 不猜。
    """

    start = Decimal(str(day_start_equity))
    current = Decimal(str(current_equity))
    if start <= 0:
        return Decimal(0)
    return max(Decimal(0), (start - current) / start)


def evaluate_risk(
    *,
    equity: Decimal | float,
    current_notional: Decimal | float,
    proposed_notional: Decimal | float,
    leverage: int,
    stop_loss: Decimal | float | None,
    entry: Decimal | float,
    daily_loss_pct: Decimal | float,
    consecutive_losses: int,
    daily_trades: int = 0,
    paused: bool,
    data_age_s: Decimal | float,
    side: str | None = None,
    is_reducing: bool = False,
    limits: RiskLimits | None = None,
    checked_at: int | None = None,
) -> RiskDecision:
    """Evaluate immutable hard limits; no model output can override this result."""

    active_limits = limits or RiskLimits()
    equity_value = Decimal(str(equity))
    current_value = abs(Decimal(str(current_notional)))
    proposed_value = abs(Decimal(str(proposed_notional)))
    entry_value = Decimal(str(entry))
    loss_value = Decimal(str(daily_loss_pct))
    age_value = Decimal(str(data_age_s))
    reasons: list[str] = []
    halting_reasons: list[str] = []
    status = RiskStatus.REJECTED

    if paused:
        return RiskDecision(
            status=RiskStatus.PAUSED,
            reasons=["paused"],
            checked_at=checked_at or int(time() * 1000),
        )
    if equity_value <= 0:
        reasons.append("equity_non_positive")
        halting_reasons.append("equity_non_positive")
    if age_value > active_limits.market_data_max_age_seconds:
        reasons.append("market_data_stale")
    # 熔断信号只看账户状态，与订单方向无关：平仓单被放行不代表亏损没发生。
    # 但这些亏损类理由以及敞口/杠杆上限只拦开新仓 —— 拦平仓会把仓位锁死，
    # 反而加大风险。
    if loss_value >= active_limits.max_daily_loss_pct:
        halting_reasons.append("daily_loss_limit")
    if consecutive_losses >= active_limits.max_consecutive_losses:
        halting_reasons.append("consecutive_loss_cooldown")
    if not is_reducing:
        reasons.extend(halting_reasons)
        # 每日开仓额度：用完了就停到明天，所以只拒单、不熔断账户。
        if daily_trades >= active_limits.max_daily_trades:
            reasons.append("daily_trade_limit")
        if leverage < 1 or leverage > active_limits.max_leverage:
            reasons.append("max_leverage")
        if equity_value > 0 and current_value + proposed_value > equity_value * active_limits.max_position_notional_pct:
            reasons.append("max_notional")

    if proposed_value > 0 and not is_reducing:
        if stop_loss is None:
            reasons.append("stop_loss_required")
        elif entry_value <= 0:
            reasons.append("entry_non_positive")
        else:
            stop_value = Decimal(str(stop_loss))
            if side and side.upper() == "LONG" and stop_value >= entry_value:
                reasons.append("long_stop_must_be_below_entry")
            if side and side.upper() == "SHORT" and stop_value <= entry_value:
                reasons.append("short_stop_must_be_above_entry")
            risk_amount = proposed_value * abs(entry_value - stop_value) / entry_value
            if equity_value > 0 and risk_amount > equity_value * active_limits.max_single_trade_risk_pct:
                reasons.append("single_trade_risk")

    if not reasons:
        status = RiskStatus.ALLOWED
    return RiskDecision(
        status=status,
        reasons=reasons,
        adjusted_position_size_pct=(float(proposed_value / equity_value) if equity_value > 0 else None),
        checked_at=checked_at or int(time() * 1000),
        halt=bool(halting_reasons),
        metadata={
            "max_leverage": active_limits.max_leverage,
            "max_position_notional_pct": str(active_limits.max_position_notional_pct),
            "max_single_trade_risk_pct": str(active_limits.max_single_trade_risk_pct),
        },
    )


class RiskEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.limits = RiskLimits.from_settings(settings or get_settings())

    def evaluate(self, **kwargs: Any) -> RiskDecision:
        return evaluate_risk(limits=self.limits, **kwargs)

"""持仓管理规则。纯函数：给一份持仓状态，返回该做什么，不做 IO。

**为什么是软件层而不是交易所侧**：交易所的止损触发单改不了也撤不掉（实测没有
cancel/modify 端点，见 docs/weex-virtual-api.md）。挂紧的止损一碰就直接平仓，
移动止损根本没机会执行 —— 所以要管的那条只能放在本地，触及后下 reduceOnly
市价单。交易所侧留着 3× 的宽灾难止损，只在 worker 挂掉时兜底。

R = |入场价 - 初始止损|。所有阈值以 R 为单位，这样不同波动率的品种可比较。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.signals.params import StrategyParams

HOLD = "HOLD"
CLOSE = "CLOSE"


@dataclass(frozen=True)
class PositionDecision:
    action: str
    effective_stop: Decimal
    peak_price: Decimal
    reason: str
    near_target_at: datetime | None


def _favorable_r(*, side: str, entry: Decimal, price: Decimal, risk: Decimal) -> Decimal:
    """浮盈有多少个 R。risk 非正时返回 0（无法度量就不触发任何阈值）。"""

    if risk <= 0:
        return Decimal(0)
    excursion = price - entry if side.upper() == "LONG" else entry - price
    return excursion / risk


def _is_stop_hit(*, side: str, price: Decimal, effective_stop: Decimal) -> bool:
    return price <= effective_stop if side.upper() == "LONG" else price >= effective_stop


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _target_price(
    *,
    side: str,
    entry: Decimal,
    risk: Decimal,
    take_profit: Decimal | None,
    reward_risk: Decimal,
) -> Decimal | None:
    """Return the persisted target, or derive one for legacy rows without it."""

    long = side.upper() == "LONG"
    if take_profit is not None:
        valid = take_profit > entry if long else take_profit < entry
        return take_profit if valid else None
    if risk <= 0 or reward_risk <= 0:
        return None
    return entry + risk * reward_risk if long else entry - risk * reward_risk


def _advanced_stop(
    *,
    side: str,
    entry: Decimal,
    effective_stop: Decimal,
    peak_price: Decimal,
    atr: Decimal | None,
    moved: bool,
    trail_multiplier: Decimal,
) -> Decimal:
    """算出新的有效止损，**只往有利方向走**。

    达到 1R 之前不动（正常回撤不该被扫）。达到之后：保本价与 1×ATR 跟随取更有利
    的那个。缺 ATR 时只做保本 —— 算不出跟随距离不等于什么都不做。
    """

    if not moved:
        return effective_stop
    candidates = [entry]
    if atr is not None and atr > 0:
        distance = atr * trail_multiplier
        trail = peak_price - distance if side.upper() == "LONG" else peak_price + distance
        candidates.append(trail)
    if side.upper() == "LONG":
        return max(effective_stop, *candidates)
    return min(effective_stop, *candidates)


def manage(
    *,
    side: str,
    entry_price: Decimal,
    initial_stop: Decimal,
    effective_stop: Decimal | None,
    peak_price: Decimal | None,
    price: Decimal,
    atr: Decimal | None,
    opened_at: datetime | None,
    now: datetime,
    signal_score: Decimal | float | None = None,
    take_profit: Decimal | None = None,
    near_target_at: datetime | None = None,
    params: StrategyParams | None = None,
) -> PositionDecision:
    """返回该对这个仓位做什么，以及更新后的持仓管理状态。"""

    active = params or StrategyParams()
    risk = abs(entry_price - initial_stop)
    stop = effective_stop if effective_stop is not None else initial_stop
    peak = peak_price if peak_price is not None else entry_price
    near_target = _as_utc(near_target_at)
    current_time = _as_utc(now) or now

    # 最有利价只在往有利方向走时更新（空仓取更低）。
    if side.upper() == "LONG":
        peak = max(peak, price)
    else:
        peak = min(peak, price)

    target = _target_price(
        side=side,
        entry=entry_price,
        risk=risk,
        take_profit=take_profit,
        reward_risk=active.reward_risk,
    )
    target_r = _favorable_r(side=side, entry=entry_price, price=target, risk=risk) if target else Decimal(0)
    favorable = _favorable_r(side=side, entry=entry_price, price=price, risk=risk)
    peak_favorable = _favorable_r(side=side, entry=entry_price, price=peak, risk=risk)

    # 1. 有效止损触及 —— 优先级最高，别的规则不该挡住它。
    if _is_stop_hit(side=side, price=price, effective_stop=stop):
        return PositionDecision(CLOSE, stop, peak, "effective_stop_hit", near_target)

    # 2. 软件止盈：用 peak 兜住采样跨过目标后又回落的情况。
    if target is not None and peak_favorable >= target_r:
        return PositionDecision(CLOSE, stop, peak, "take_profit", near_target)

    # 3. 结构失效：当初开仓的理由没了。
    if signal_score is not None:
        score = Decimal(str(signal_score))
        invalidated = score <= 0 if side.upper() == "LONG" else score >= 0
        if invalidated:
            return PositionDecision(CLOSE, stop, peak, "structure_invalidated", near_target)

    # 首次进入 near-target 后锁存时间；即使随后回撤，也不能把这次进展忘掉。
    if (
        near_target is None
        and active.near_target_r > 0
        and target_r > active.near_target_r
        and peak_favorable >= active.near_target_r
    ):
        near_target = current_time

    opened_time = _as_utc(opened_at)
    if near_target is not None and active.near_target_timeout_hours >= 0:
        near_target_age = (current_time - near_target).total_seconds() / 3600
        if near_target_age >= active.near_target_timeout_hours:
            return PositionDecision(CLOSE, stop, peak, "near_target_timeout", near_target)

    if opened_time is not None:
        # SQLite 取回来的 timestamp 是 naive 的（Postgres 是 aware）—— 统一按 UTC
        # 处理，否则相减会炸。这个差异只在测试库出现，但代码不该依赖它。
        held_hours = (current_time - opened_time).total_seconds() / 3600
        if active.max_hold_hours > 0 and held_hours >= active.max_hold_hours:
            return PositionDecision(CLOSE, stop, peak, "max_hold", near_target)
        if held_hours >= active.time_stop_hours and favorable < active.time_stop_min_r:
            return PositionDecision(CLOSE, stop, peak, "time_stop", near_target)

    # 4. 保本 / 移动止损。
    moved = favorable >= active.breakeven_r
    return PositionDecision(
        HOLD,
        _advanced_stop(
            side=side,
            entry=entry_price,
            effective_stop=stop,
            peak_price=peak,
            atr=atr,
            moved=moved,
            trail_multiplier=active.trail_atr_multiplier,
        ),
        peak,
        "trailing_updated" if moved else "holding",
        near_target,
    )

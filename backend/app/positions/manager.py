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


def _favorable_r(*, side: str, entry: Decimal, price: Decimal, risk: Decimal) -> Decimal:
    """浮盈有多少个 R。risk 非正时返回 0（无法度量就不触发任何阈值）。"""

    if risk <= 0:
        return Decimal(0)
    excursion = price - entry if side.upper() == "LONG" else entry - price
    return excursion / risk


def _is_stop_hit(*, side: str, price: Decimal, effective_stop: Decimal) -> bool:
    return price <= effective_stop if side.upper() == "LONG" else price >= effective_stop


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
    params: StrategyParams | None = None,
) -> PositionDecision:
    """返回该对这个仓位做什么，以及更新后的有效止损 / 最有利价。"""

    active = params or StrategyParams()
    risk = abs(entry_price - initial_stop)
    stop = effective_stop if effective_stop is not None else initial_stop
    peak = peak_price if peak_price is not None else entry_price

    # 最有利价只在往有利方向走时更新（空仓取更低）。
    if side.upper() == "LONG":
        peak = max(peak, price)
    else:
        peak = min(peak, price)

    # 1. 有效止损触及 —— 优先级最高，别的规则不该挡住它。
    if _is_stop_hit(side=side, price=price, effective_stop=stop):
        return PositionDecision(CLOSE, stop, peak, "effective_stop_hit")

    # 2. 结构失效：当初开仓的理由没了。
    if signal_score is not None:
        score = Decimal(str(signal_score))
        invalidated = score <= 0 if side.upper() == "LONG" else score >= 0
        if invalidated:
            return PositionDecision(CLOSE, stop, peak, "structure_invalidated")

    # 3. 时间止损：占着风险预算却不赚钱。
    favorable = _favorable_r(side=side, entry=entry_price, price=price, risk=risk)
    if opened_at is not None:
        # SQLite 取回来的 timestamp 是 naive 的（Postgres 是 aware）—— 统一按 UTC
        # 处理，否则相减会炸。这个差异只在测试库出现，但代码不该依赖它。
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=UTC)
        held_hours = (now - opened_at).total_seconds() / 3600
        if held_hours >= active.time_stop_hours and favorable < active.time_stop_min_r:
            return PositionDecision(CLOSE, stop, peak, "time_stop")

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
    )

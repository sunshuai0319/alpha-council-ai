from decimal import Decimal

from app.domain.enums import RiskStatus
from app.risk.engine import RiskLimits, daily_loss_pct, evaluate_risk


def _risk(**overrides):
    base = {
        "equity": 10000,
        "current_notional": 0,
        "proposed_notional": 1000,
        "leverage": 2,
        "stop_loss": 95,
        "entry": 100,
        "daily_loss_pct": 0,
        "consecutive_losses": 0,
        "paused": False,
        "data_age_s": 5,
    }
    return evaluate_risk(**{**base, **overrides})


def test_account_level_breakers_request_a_halt() -> None:
    """日亏损/连亏是账户级状态，要升级成暂停，而不只是拒掉这一单。"""
    assert _risk(daily_loss_pct=0.06).halt is True
    assert _risk(consecutive_losses=3).halt is True
    assert _risk(equity=0).halt is True


def test_order_level_rejections_do_not_halt_the_account() -> None:
    """单笔问题（止损缺失、仓位过大、行情过期）只拒这一单。"""
    assert _risk(stop_loss=None).halt is False
    assert _risk(current_notional=5000).halt is False
    assert _risk(data_age_s=10_000).halt is False


def test_reducing_orders_are_allowed_through_loss_breakers() -> None:
    """触发亏损熔断时必须还能平仓，否则仓位会被永久锁死。"""
    decision = _risk(daily_loss_pct=0.06, consecutive_losses=3, is_reducing=True)

    assert decision.allowed is True
    assert decision.reasons == []
    assert decision.halt is True  # 仍然要求暂停开新仓


def test_daily_trade_limit_blocks_further_entries() -> None:
    """每日开仓次数上限：防失控换手。它是当日额度，不是账户级熔断。"""
    limits = RiskLimits(max_daily_trades=20)
    decision = _risk(daily_trades=20, limits=limits)

    assert decision.allowed is False
    assert "daily_trade_limit" in decision.reasons
    assert decision.halt is False


def test_daily_trade_limit_still_allows_reducing() -> None:
    limits = RiskLimits(max_daily_trades=20)
    assert _risk(daily_trades=20, is_reducing=True, limits=limits).allowed is True


def test_account_limits_can_only_tighten_the_platform_limits() -> None:
    """用户只能把风控调得更严。

    调松等于剪掉自己的安全绳，所以超出平台上限的取值一律取平台值。
    """
    platform = RiskLimits(
        max_position_notional_pct=Decimal("0.20"),
        max_daily_loss_pct=Decimal("0.05"),
    )

    tightened = platform.tightened(
        {"max_position_notional_pct": 0.10, "max_daily_loss_pct": 0.50}
    )

    assert tightened.max_position_notional_pct == Decimal("0.10")
    assert tightened.max_daily_loss_pct == Decimal("0.05")


def test_account_limits_leave_unspecified_values_alone() -> None:
    platform = RiskLimits(max_leverage=20)

    tightened = platform.tightened({"max_daily_loss_pct": 0.01})

    assert tightened.max_daily_loss_pct == Decimal("0.01")
    assert tightened.max_leverage == 20  # 没提到的保持平台值
    assert tightened.max_consecutive_losses == platform.max_consecutive_losses


def test_no_account_limits_means_platform_limits() -> None:
    platform = RiskLimits()
    assert platform.tightened(None) is platform
    assert platform.tightened({}) is platform


def test_daily_loss_pct_measures_drawdown_from_day_start() -> None:
    assert daily_loss_pct(day_start_equity=Decimal(10000), current_equity=Decimal(9500)) == Decimal("0.05")


def test_daily_loss_pct_is_zero_when_in_profit_or_flat() -> None:
    assert daily_loss_pct(day_start_equity=Decimal(10000), current_equity=Decimal(10500)) == Decimal(0)
    assert daily_loss_pct(day_start_equity=Decimal(10000), current_equity=Decimal(10000)) == Decimal(0)


def test_daily_loss_pct_needs_a_positive_baseline() -> None:
    """没有基准（权益为 0）时不能除零，也不能假装亏损。"""
    assert daily_loss_pct(day_start_equity=Decimal(0), current_equity=Decimal(0)) == Decimal(0)


def test_risk_rejects_position_above_equity_limit() -> None:
    decision = evaluate_risk(
        equity=10000,
        current_notional=1500,
        proposed_notional=1000,
        leverage=2,
        stop_loss=95,
        entry=100,
        daily_loss_pct=0,
        consecutive_losses=0,
        paused=False,
        data_age_s=5,
    )
    assert decision.allowed is False
    assert "max_notional" in decision.reasons


def test_risk_rejects_entry_without_stop_loss_or_with_excessive_loss() -> None:
    missing_stop = evaluate_risk(
        equity=10000,
        current_notional=0,
        proposed_notional=1000,
        leverage=2,
        stop_loss=None,
        entry=100,
        daily_loss_pct=0,
        consecutive_losses=0,
        paused=False,
        data_age_s=5,
    )
    excessive_loss = evaluate_risk(
        equity=10000,
        current_notional=0,
        proposed_notional=1000,
        leverage=2,
        stop_loss=80,
        entry=100,
        daily_loss_pct=0,
        consecutive_losses=0,
        paused=False,
        data_age_s=5,
    )
    assert "stop_loss_required" in missing_stop.reasons
    assert "single_trade_risk" in excessive_loss.reasons


def test_risk_pause_and_circuit_breaker_have_priority() -> None:
    decision = evaluate_risk(
        equity=10000,
        current_notional=0,
        proposed_notional=100,
        leverage=2,
        stop_loss=99,
        entry=100,
        daily_loss_pct=0.06,
        consecutive_losses=3,
        paused=True,
        data_age_s=200,
    )
    assert decision.status is RiskStatus.PAUSED
    assert decision.reasons == ["paused"]

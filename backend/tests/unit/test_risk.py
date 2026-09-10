from decimal import Decimal

from app.domain.enums import RiskStatus
from app.risk.engine import daily_loss_pct, evaluate_risk


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

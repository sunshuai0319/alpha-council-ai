from app.domain.enums import RiskStatus
from app.risk.engine import evaluate_risk


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

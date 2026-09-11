"""策略参数外置：改参不用改代码，回测才能扫参数。"""

from decimal import Decimal

from app.config import Settings
from app.signals.params import StrategyParams


def test_defaults_match_the_values_the_modules_shipped_with() -> None:
    """默认值必须与重构前的模块常量逐位一致 —— 这一步是纯重构，不改行为。"""
    params = StrategyParams()
    assert params.trend_weight == 0.40
    assert params.momentum_weight == 0.25
    assert params.volume_weight == 0.15
    assert params.entry_threshold == 0.35
    assert params.atr_multiplier == Decimal("1.5")
    assert params.reward_risk == Decimal("2.0")
    assert params.disaster_multiplier == Decimal("3.0")
    assert params.breakeven_r == Decimal("1.0")
    assert params.trail_atr_multiplier == Decimal("1.0")
    assert params.time_stop_hours == 48
    assert params.time_stop_min_r == Decimal("0.3")


def test_params_can_be_overridden_per_run() -> None:
    """回测要能逐组扫参数。"""
    params = StrategyParams(entry_threshold=0.5, atr_multiplier=Decimal("2.5"))
    assert params.entry_threshold == 0.5
    assert params.atr_multiplier == Decimal("2.5")


def test_from_settings_reads_the_risk_budget_from_the_existing_limits() -> None:
    """风险预算不另开一套字段 —— 与 RiskLimits 共用，避免两个真相来源。"""
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        max_single_trade_risk_pct=0.004,
        max_position_notional_pct=0.15,
    )
    params = StrategyParams.from_settings(settings)

    assert params.risk_pct == Decimal("0.004")
    assert params.max_notional_pct == Decimal("0.15")


def test_from_settings_allows_env_style_overrides_of_signal_params() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        STRATEGY_ENTRY_THRESHOLD=0.6,
        STRATEGY_ATR_MULTIPLIER=2.0,
        STRATEGY_REWARD_RISK=3.0,
    )
    params = StrategyParams.from_settings(settings)

    assert params.entry_threshold == 0.6
    assert params.atr_multiplier == Decimal(2)
    assert params.reward_risk == Decimal(3)


def test_from_settings_leaves_unset_params_at_their_defaults() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )
    assert StrategyParams.from_settings(settings) == StrategyParams()

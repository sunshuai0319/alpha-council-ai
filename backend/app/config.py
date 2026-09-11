from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    postgres_url: str = Field(validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"))
    milvus_uri: str = Field(validation_alias=AliasChoices("MILVUS_URI"))
    milvus_user: str = ""
    milvus_password: str = ""
    milvus_token: str = ""
    milvus_db_name: str = ""
    milvus_collection: str = "alpha_council_documents_bge_m3_v1"

    embedding_model_path: str = ""
    reranker_model_path: str = ""

    ark_api_key: str = Field(validation_alias=AliasChoices("ARK_API_KEY"))
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_model: str = "deepseek-v4-pro-ga-260813"
    ark_timeout_seconds: float = 60
    #: 瞬时故障（读超时 / 网络抖动 / 5xx）的重试次数。LLM 生成幂等无副作用，
    #: 重发一次通常就成功 —— 实测三个 agent 并行时偶发 ReadTimeout，整个分析
    #: 却因此退化。4xx / 解析失败不属于瞬时故障，不重试。
    ark_retry_attempts: int = 2
    ark_retry_backoff_seconds: float = 1.0

    weex_base_url: str = "https://api-contract.weex.com"
    weex_virtual_only: bool = True

    clerk_jwks_url: str = ""
    clerk_issuer: str = ""
    clerk_audience: str = ""
    clerk_secret_key: str = ""
    clerk_webhook_signing_secret: str = ""

    trading_enabled: bool = True
    #: 平台硬上限。虚拟盘实际杠杆固定 20x 且不可调，上限必须与之对齐，
    #: 否则虚拟盘一开仓就再也无法加仓（max_leverage 会拒掉）。
    max_leverage: int = 20
    max_position_notional_pct: float = 0.20
    max_single_trade_risk_pct: float = 0.005
    max_daily_loss_pct: float = 0.05
    max_consecutive_losses: int = 3
    max_consecutive_failures: int = 5
    max_daily_trades: int = 20
    market_data_max_age_seconds: int = 90
    min_reward_risk: float = 1.5

    #: 策略参数（打分卡 + 仓位 + 持仓管理）。放在这里是为了改参不用改代码 ——
    #: 默认值就是系统一直在用的值，见 app/signals/params.py。
    #: 风险预算不在这里另开字段：它与 max_single_trade_risk_pct /
    #: max_position_notional_pct 是同一个概念，共用那一对，避免两个真相来源。
    #: 打分卡读哪两个周期。拉长周期会同时降频并压低手续费占比 ——
    #: 手续费/R = 2×费率÷止损距离占比，止损距离是百分比，周期越大幅度越大。
    strategy_entry_timeframe: str = "1h"
    strategy_trend_timeframe: str = "4h"
    #: 每轮采集哪些周期（逗号分隔）。换策略周期时要一起改，否则指标里没有
    #: 打分卡需要的 key。WEEX 合法值：1m/5m/15m/30m/1h/4h/12h/1d/1w（2h/6h 不支持）。
    market_timeframes: str = "5m,1h,4h"

    strategy_trend_weight: float = 0.40
    strategy_momentum_weight: float = 0.25
    strategy_volume_weight: float = 0.15
    strategy_entry_threshold: float = 0.35
    strategy_atr_multiplier: float = 1.5
    strategy_reward_risk: float = 2.0
    strategy_disaster_multiplier: float = 3.0
    strategy_breakeven_r: float = 1.0
    strategy_trail_atr_multiplier: float = 1.0
    strategy_time_stop_hours: int = 48
    strategy_time_stop_min_r: float = 0.3
    decision_interval_seconds: int = 300

    #: FRED 序列的重抓间隔，按各自更新频率决定。月度数据按分钟级频率重抓没有
    #: 收益，还会招来 503。
    fred_monthly_interval_seconds: int = 86400
    fred_daily_interval_seconds: int = 3600

    @property
    def timeframe_list(self) -> tuple[str, ...]:
        """`market_timeframes` 的解析结果。

        pydantic-settings 读 `list[str]` 默认要 JSON，所以用逗号分隔的字符串，
        在这里拆开 —— 配置起来更直观，也和 .env 的写法一致。
        """

        return tuple(part.strip() for part in self.market_timeframes.split(",") if part.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

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

    weex_base_url: str = "https://api-contract.weex.com"
    weex_api_key: str = ""
    weex_api_secret: str = ""
    weex_api_passphrase: str = ""
    weex_virtual_only: bool = True

    clerk_jwks_url: str = ""
    clerk_issuer: str = ""
    clerk_audience: str = ""
    clerk_secret_key: str = ""
    clerk_webhook_signing_secret: str = ""

    max_leverage: int = 3
    max_position_notional_pct: float = 0.20
    max_single_trade_risk_pct: float = 0.005
    max_daily_loss_pct: float = 0.05
    max_consecutive_losses: int = 3
    market_data_max_age_seconds: int = 90
    decision_interval_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

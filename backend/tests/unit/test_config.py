from app.config import Settings


def test_settings_require_external_service_urls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/a")
    monkeypatch.setenv("ZILLIZ_URI", "https://zilliz.example")
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    settings = Settings()
    assert settings.zilliz_uri == "https://zilliz.example"
    assert settings.postgres_url.startswith("postgresql+psycopg://")


def test_trading_symbols_are_configurable() -> None:
    """品种列表原来写死在 workers/scheduler.py 里。"""
    from app.config import Settings

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        TRADING_SYMBOLS="BTC-USDT, SOL-USDT ,XRP-USDT",
    )
    assert settings.symbol_list == ("BTC-USDT", "SOL-USDT", "XRP-USDT")


def test_default_symbols_are_unchanged() -> None:
    from app.config import Settings

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )
    assert settings.symbol_list == ("BTC-USDT", "ETH-USDT")


def test_exchange_take_profit_is_enabled_by_default() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )

    assert settings.exchange_take_profit_enabled is True


def test_exchange_take_profit_can_be_disabled_by_configuration() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        EXCHANGE_TAKE_PROFIT_ENABLED=False,
    )

    assert settings.exchange_take_profit_enabled is False


def test_trading_defaults_to_lark_notification_mode_and_can_restore_execution() -> None:
    default_settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )
    execute_settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        TRADING_EXECUTION_MODE="execute",
    )

    assert default_settings.trading_execution_mode == "notify"
    assert execute_settings.trading_execution_mode == "execute"


def test_reentry_cooldown_defaults_to_six_hours_and_is_configurable() -> None:
    default_settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        REENTRY_COOLDOWN_SECONDS=900,
    )

    assert default_settings.reentry_cooldown_seconds == 6 * 60 * 60
    assert settings.reentry_cooldown_seconds == 900


def test_zilliz_is_the_only_vector_store() -> None:
    """本地 Milvus 配置已整体移除，不再有 MILVUS_* / USE_ZILLIZ 这套开关。"""

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="https://zilliz.example",
        ZILLIZ_TOKEN="zilliz-token",
        ZILLIZ_USER="zilliz-user",
        ZILLIZ_PASSWORD="zilliz-password",
        ZILLIZ_DB_NAME="zilliz-db",
        ZILLIZ_COLLECTION="zilliz_collection",
        ARK_API_KEY="test-key",
    )

    assert settings.zilliz_uri == "https://zilliz.example"
    assert settings.zilliz_token == "zilliz-token"
    assert settings.zilliz_user == "zilliz-user"
    assert settings.zilliz_password == "zilliz-password"
    assert settings.zilliz_db_name == "zilliz-db"
    assert settings.zilliz_collection == "zilliz_collection"

    for removed in ("milvus_uri", "milvus_collection", "use_zilliz", "vector_store_uri"):
        assert not hasattr(settings, removed), f"{removed} 应当已被移除"


def test_local_embedding_settings_are_removed() -> None:
    """嵌入只剩 Doubao（本地 BGE/reranker 已移除），不该再有 provider 开关和本地模型路径。"""

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="https://zilliz.example",
        ARK_API_KEY="test-key",
    )

    for removed in (
        "embedding_provider",
        "embedding_model_name",
        "embedding_dimension",
        "embedding_model_path",
        "reranker_model_path",
        "active_embedding_dimension",
    ):
        assert not hasattr(settings, removed), f"{removed} 应当已被移除"

    assert settings.doubao_embedding_dimension == 1024

from app.config import Settings


def test_settings_require_external_service_urls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/a")
    monkeypatch.setenv("MILVUS_URI", "http://milvus:19530")
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    settings = Settings()
    assert settings.milvus_uri == "http://milvus:19530"
    assert settings.postgres_url.startswith("postgresql+psycopg://")


def test_trading_symbols_are_configurable() -> None:
    """品种列表原来写死在 workers/scheduler.py 里。"""
    from app.config import Settings

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        TRADING_SYMBOLS="BTC-USDT, SOL-USDT ,XRP-USDT",
    )
    assert settings.symbol_list == ("BTC-USDT", "SOL-USDT", "XRP-USDT")


def test_default_symbols_are_unchanged() -> None:
    from app.config import Settings

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )
    assert settings.symbol_list == ("BTC-USDT", "ETH-USDT")


def test_exchange_take_profit_is_enabled_by_default() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )

    assert settings.exchange_take_profit_enabled is True


def test_exchange_take_profit_can_be_disabled_by_configuration() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        EXCHANGE_TAKE_PROFIT_ENABLED=False,
    )

    assert settings.exchange_take_profit_enabled is False


def test_zilliz_vector_store_settings_are_selected_when_enabled() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        MILVUS_USER="local-user",
        MILVUS_PASSWORD="local-password",
        MILVUS_DB_NAME="default",
        MILVUS_COLLECTION="local_collection",
        ARK_API_KEY="test-key",
        USE_ZILLIZ=True,
        ZILLIZ_URI="https://zilliz.example",
        ZILLIZ_TOKEN="zilliz-token",
        ZILLIZ_USER="zilliz-user",
        ZILLIZ_PASSWORD="zilliz-password",
        ZILLIZ_DB_NAME="zilliz-db",
        ZILLIZ_COLLECTION="zilliz_collection",
    )

    assert settings.vector_store_uri == "https://zilliz.example"
    assert settings.vector_store_token == "zilliz-token"
    assert settings.vector_store_user == "zilliz-user"
    assert settings.vector_store_password == "zilliz-password"
    assert settings.vector_store_db_name == "zilliz-db"
    assert settings.vector_store_collection == "zilliz_collection"


def test_zilliz_vector_store_settings_default_to_local_milvus() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        MILVUS_TOKEN="local-token",
        MILVUS_COLLECTION="local_collection",
        ARK_API_KEY="test-key",
        USE_ZILLIZ=False,
        ZILLIZ_URI="https://zilliz.example",
        ZILLIZ_TOKEN="zilliz-token",
        ZILLIZ_COLLECTION="zilliz_collection",
    )

    assert settings.vector_store_uri == "http://localhost:19530"
    assert settings.vector_store_token == "local-token"
    assert settings.vector_store_collection == "local_collection"

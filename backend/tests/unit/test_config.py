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

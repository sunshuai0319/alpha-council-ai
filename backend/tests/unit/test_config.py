from app.config import Settings


def test_settings_require_external_service_urls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/a")
    monkeypatch.setenv("MILVUS_URI", "http://milvus:19530")
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    settings = Settings()
    assert settings.milvus_uri == "http://milvus:19530"
    assert settings.postgres_url.startswith("postgresql+psycopg://")

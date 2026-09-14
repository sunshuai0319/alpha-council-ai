from app.config import Settings
from scripts.reindex_documents import default_target_collection, target_settings


def test_default_target_collection_is_non_destructive() -> None:
    assert default_target_collection("alpha_council_documents_bge_m3_v1") == "alpha_council_documents_bge_m3_v2"
    assert default_target_collection("documents") == "documents_v2"


def test_target_settings_switches_only_the_destination_schema() -> None:
    settings = Settings(
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        MILVUS_COLLECTION="alpha_council_documents_bge_m3_v1",
        MILVUS_SCHEMA_VERSION="v1",
    )
    updated = target_settings(settings, "documents_v2")
    assert settings.milvus_collection == "alpha_council_documents_bge_m3_v1"
    assert updated.milvus_collection == "documents_v2"
    assert updated.milvus_schema_version == "v2"

from app.config import Settings
from scripts.reindex_documents import (
    default_target_collection,
    dry_run_plan,
    format_plan,
    target_settings,
)


def test_default_target_collection_is_non_destructive() -> None:
    assert default_target_collection("alpha_council_documents_bge_m3_v2") == "alpha_council_documents_doubao_vision_v1"
    assert default_target_collection("documents") == "documents_doubao_v1"


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
    assert updated.embedding_provider == "doubao"
    assert updated.active_embedding_dimension == 1024


def test_dry_run_plan_reports_source_target_provider_and_dimension() -> None:
    settings = Settings(
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        MILVUS_COLLECTION="alpha_council_documents_bge_m3_v1",
    )
    plan = dry_run_plan(settings, "alpha_council_documents_doubao_vision_v1", selected=116)

    assert plan == {
        "source_collection": "alpha_council_documents_bge_m3_v1",
        "target_collection": "alpha_council_documents_doubao_vision_v1",
        "provider": "doubao",
        "dimension": 1024,
        "selected": 116,
    }
    assert format_plan(plan) == (
        "source_collection=alpha_council_documents_bge_m3_v1 "
        "target_collection=alpha_council_documents_doubao_vision_v1 "
        "provider=doubao dimension=1024 selected=116"
    )

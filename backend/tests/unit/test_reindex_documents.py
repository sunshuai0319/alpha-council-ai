import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db.models import Base
from scripts.reindex_documents import (
    default_target_collection,
    dry_run_plan,
    format_plan,
    run,
    target_settings,
)


def _settings(**overrides) -> Settings:
    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "ZILLIZ_URI": "https://zilliz.example.test",
        "ARK_API_KEY": "test-key",
        "ZILLIZ_COLLECTION": "alpha_council_documents_doubao_vision_v1",
    }
    values.update(overrides)
    return Settings(**values)


def test_default_target_collection_is_non_destructive() -> None:
    assert default_target_collection("alpha_council_documents_bge_m3_v2") == "alpha_council_documents_doubao_vision_v1"
    assert default_target_collection("documents") == "documents_doubao_v1"


def test_target_settings_switches_only_the_destination_schema() -> None:
    settings = Settings(
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        ZILLIZ_URI="https://zilliz.example.test",
        ARK_API_KEY="test-key",
        ZILLIZ_COLLECTION="alpha_council_documents_bge_m3_v2",
        MILVUS_SCHEMA_VERSION="v1",
    )
    updated = target_settings(settings, "documents_v2")
    assert settings.zilliz_collection == "alpha_council_documents_bge_m3_v2"
    assert updated.zilliz_collection == "documents_v2"
    assert updated.milvus_schema_version == "v2"
    assert updated.doubao_embedding_dimension == 1024


def test_dry_run_plan_reports_source_target_provider_and_dimension() -> None:
    settings = Settings(
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        ZILLIZ_URI="https://zilliz.example.test",
        ARK_API_KEY="test-key",
        ZILLIZ_COLLECTION="alpha_council_documents_bge_m3_v2",
    )
    plan = dry_run_plan(settings, "alpha_council_documents_doubao_vision_v1", selected=116)

    assert plan == {
        "source_collection": "alpha_council_documents_bge_m3_v2",
        "target_collection": "alpha_council_documents_doubao_vision_v1",
        "provider": "doubao",
        "dimension": 1024,
        "selected": 116,
    }
    assert format_plan(plan) == (
        "source_collection=alpha_council_documents_bge_m3_v2 "
        "target_collection=alpha_council_documents_doubao_vision_v1 "
        "provider=doubao dimension=1024 selected=116"
    )


def test_dry_run_still_runs_when_the_target_is_the_live_collection(tmp_path) -> None:
    """切到新集合后默认目标就等于当前集合。dry-run 只读，不该因此拒绝执行 ——
    否则回填完就再也跑不了诊断命令了。"""

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reindex.db'}")
    Base.metadata.create_all(engine)
    settings = _settings()

    stats = run(
        settings=settings,
        target_collection=settings.zilliz_collection,
        dry_run=True,
        session_factory=sessionmaker(bind=engine, autoflush=False),
    )

    assert stats.selected == 0
    assert stats.indexed == 0


def test_real_run_still_refuses_to_write_into_the_live_collection() -> None:
    """真正写库时必须要求目标不同于当前集合，避免覆盖线上集合。"""

    def unreachable_session():  # pragma: no cover - 保护先于任何数据库访问
        raise AssertionError("保护生效时不应触碰数据库")

    settings = _settings()
    with pytest.raises(ValueError, match="must differ from the current collection"):
        run(
            settings=settings,
            target_collection=settings.zilliz_collection,
            session_factory=unreachable_session,
        )

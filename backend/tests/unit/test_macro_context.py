from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, MacroObservationRecord, SourceDocument
from app.services.context import load_macro_context


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ctx.db'}")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_macro_context_reports_latest_value_and_change(tmp_path):
    """宏观 agent 需要的是「现在多少、比上期变了多少」，不是原始行列表。"""
    with _session(tmp_path) as db:
        base = datetime(2026, 7, 1, tzinfo=UTC)
        for offset, value in ((0, 332.5), (1, 332.8)):
            db.add(MacroObservationRecord(
                series_id="CPIAUCSL",
                observation_date=base + timedelta(days=30 * offset),
                value=value,
                source_url="https://fred.example/CPIAUCSL",
                fetched_at=base,
            ))
        db.commit()

        context = load_macro_context(db, limit=10)

    series = {item["series_id"]: item for item in context if item.get("kind") == "macro_series"}
    assert series["CPIAUCSL"]["value"] == 332.8
    assert series["CPIAUCSL"]["previous_value"] == 332.5
    assert round(series["CPIAUCSL"]["change"], 4) == 0.3


def test_macro_context_skips_null_values(tmp_path):
    """解析修好之前写进库的 NULL 行不能污染上下文。"""
    with _session(tmp_path) as db:
        db.add(MacroObservationRecord(
            series_id="DGS10",
            observation_date=datetime(2026, 7, 1, tzinfo=UTC),
            value=None,
            source_url="https://fred.example/DGS10",
            fetched_at=datetime.now(UTC),
        ))
        db.commit()

        assert load_macro_context(db, limit=10) == []


def test_macro_context_includes_fed_press_documents(tmp_path):
    """FED 官方新闻走的是 source_documents，不是 macro_observations。"""
    with _session(tmp_path) as db:
        db.add(SourceDocument(
            id="doc-1",
            source="federal-reserve",
            canonical_url="https://fed.example/1",
            title="Fed holds rates",
            raw_text="text",
            cleaned_text="text",
            content_hash="hash-1",
            language="en",
            published_at=datetime.now(UTC) - timedelta(hours=2),
            fetched_at=datetime.now(UTC),
            processing_status="INDEXED",
        ))
        db.commit()

        context = load_macro_context(db, limit=10)

    press = [item for item in context if item.get("kind") == "fed_press"]
    assert len(press) == 1
    assert press[0]["title"] == "Fed holds rates"


def test_macro_context_ignores_stale_fed_press(tmp_path):
    """七周前的 FED 新闻对短线决策没有意义，不该塞进上下文。"""
    with _session(tmp_path) as db:
        db.add(SourceDocument(
            id="doc-old",
            source="federal-reserve",
            canonical_url="https://fed.example/old",
            title="Ancient statement",
            raw_text="text",
            cleaned_text="text",
            content_hash="hash-old",
            language="en",
            published_at=datetime.now(UTC) - timedelta(days=60),
            fetched_at=datetime.now(UTC),
            processing_status="INDEXED",
        ))
        db.commit()

        assert load_macro_context(db, limit=10) == []


def test_macro_context_is_empty_when_nothing_is_stored(tmp_path):
    with _session(tmp_path) as db:
        assert load_macro_context(db, limit=10) == []


def test_macro_context_tolerates_a_missing_session():
    """库不可用时返回空列表：宏观缺失只该让 agent 说「数据不足」，不该让周期崩。"""
    assert load_macro_context(None, limit=10) == []

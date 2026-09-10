from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.collectors.base import CollectorResult
from app.collectors.macro import MacroEvent, MacroObservation
from app.collectors.rss import NewsItem
from app.db.models import Base, DocumentSummary, MacroObservationRecord, SourceDocument
from app.processing.ark import DocumentSummary as Summary
from app.workers.pipeline import DocumentPipeline


class FakeRSS:
    def collect(self):
        return CollectorResult(
            source="rss",
            items=[
                NewsItem(
                    source="test",
                    canonical_url="https://example.com/news?utm_source=x",
                    title="Bitcoin ETF update",
                    summary="Bitcoin market event",
                    content_hash="hash",
                    published_at=datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                )
            ],
        )


class FakeMacro:
    def collect(self):
        return (
            CollectorResult(
                source="fred",
                items=[
                    MacroObservation(
                        series_id="DFF",
                        observation_date=datetime.now(UTC).date(),
                        value=5.1,
                        source_url="https://fred.example/DFF",
                        fetched_at=datetime.now(UTC),
                    )
                ],
            ),
            CollectorResult(
                source="fed",
                items=[
                    MacroEvent(
                        event_id="fed-1",
                        event_type="FED_PRESS",
                        title="Fed statement",
                        summary="Rates unchanged",
                        source_url="https://fed.example/1",
                        published_at=datetime.now(UTC),
                        fetched_at=datetime.now(UTC),
                    )
                ],
            ),
        )


class FakeSummary:
    def summarize(self, document):
        return Summary(
            summary=f"summary:{document.title}",
            event_type=document.event_type,
            assets=list(document.assets),
            direction="neutral",
            impact_horizon="short",
            confidence=0.8,
        )


class FakeEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeIndexer:
    def __init__(self):
        self.chunks = []

    def insert(self, chunks):
        self.chunks.extend(chunks)


def test_document_pipeline_persists_summary_macro_observation_and_indexes(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)
    indexer = FakeIndexer()
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=FakeSummary(),
            embedder=FakeEmbedder(),
            indexer=indexer,
        )
        result = pipeline.run_once()
        assert result["processed"] == 2
        assert len(db.scalars(select(SourceDocument)).all()) == 2
        assert len(db.scalars(select(DocumentSummary)).all()) == 2
        assert len(db.scalars(select(MacroObservationRecord)).all()) == 1
        assert indexer.chunks


def test_document_pipeline_commits_with_autoflush_off_and_foreign_keys_enforced(tmp_path):
    """autoflush=False（与 worker 的 SessionLocal 一致）时，父表与子表在同一 flush 中
    一起插入，必须保证 source_documents 先于 document_summaries，否则外键违例。"""
    from sqlalchemy import event

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pipeline_fk.db'}")

    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(engine, "connect", _fk_on)
    Base.metadata.create_all(engine)
    indexer = FakeIndexer()
    with Session(engine, autoflush=False) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=FakeSummary(),
            embedder=FakeEmbedder(),
            indexer=indexer,
        )
        result = pipeline.run_once()
        assert result["processed"] == 2
        assert len(db.scalars(select(SourceDocument)).all()) == 2
        assert len(db.scalars(select(DocumentSummary)).all()) == 2

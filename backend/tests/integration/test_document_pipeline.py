from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.collectors.base import CollectorResult
from app.collectors.macro import MacroEvent, MacroObservation
from app.collectors.rss import NewsItem
from app.config import Settings
from app.db.models import (
    Base,
    CollectorError,
    DocumentSummary,
    MacroObservationRecord,
    SourceDocument,
)
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
    def collect(self, series_ids=(), fred_limit=30, fed_feed_url=None):
        del series_ids, fred_limit, fed_feed_url
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


class RecordingMacro:
    """只记录被要求抓取的序列，不产生数据。"""

    def __init__(self) -> None:
        self.requested: list[tuple[str, ...]] = []

    def collect(self, series_ids=(), fred_limit=30, fed_feed_url=None):
        del fred_limit, fed_feed_url
        self.requested.append(tuple(series_ids))
        return CollectorResult(source="fred", succeeded=list(series_ids)), CollectorResult(
            source="federal-reserve"
        )


class FlakyMacro(RecordingMacro):
    """每个被请求的序列都失败。"""

    def collect(self, series_ids=(), fred_limit=30, fed_feed_url=None):
        super().collect(series_ids, fred_limit, fed_feed_url)
        return (
            CollectorResult(source="fred", errors=[f"{series}: boom" for series in series_ids]),
            CollectorResult(source="federal-reserve"),
        )


def test_macro_series_are_only_refetched_when_due(tmp_path):
    """月度序列不需要每 5 分钟重抓。

    FRED 的月度序列按调度间隔重抓是纯浪费（约 1440 次/天），也是整批 503 的
    可疑来源。日度序列按小时，月度按天。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'schedule.db'}")
    Base.metadata.create_all(engine)
    now = [0.0]
    macro = RecordingMacro()
    settings = Settings(fred_monthly_interval_seconds=86400, fred_daily_interval_seconds=3600)
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeEmptyRSS(),
            macro=macro,
            settings=settings,
            clock=lambda: now[0],
        )

        pipeline.run_once()
        assert set(macro.requested[0]) == {"FEDFUNDS", "CPIAUCSL", "UNRATE", "DFF", "DGS10"}

        now[0] += 300  # 一个调度周期之后
        pipeline.run_once()
        assert macro.requested[1] == ()

        now[0] += 3600  # 累计 65 分钟：日度到期，月度还没到
        pipeline.run_once()
        assert set(macro.requested[2]) == {"DFF", "DGS10"}


def test_failed_series_is_retried_on_the_next_run(tmp_path):
    """抓取失败不能把该源锁到下一个间隔：短暂故障不该让数据延迟一整天。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'retry.db'}")
    Base.metadata.create_all(engine)
    now = [0.0]
    macro = FlakyMacro()
    settings = Settings(fred_monthly_interval_seconds=86400, fred_daily_interval_seconds=3600)
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db, rss=FakeEmptyRSS(), macro=macro, settings=settings, clock=lambda: now[0]
        )

        pipeline.run_once()
        now[0] += 300
        pipeline.run_once()

        assert set(macro.requested[1]) == {"FEDFUNDS", "CPIAUCSL", "UNRATE", "DFF", "DGS10"}


def test_collector_errors_are_persisted(tmp_path):
    """错误只写日志就无法回答"网络到底稳不稳"：翻日志数不出错误率。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'errors.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(fred_monthly_interval_seconds=86400, fred_daily_interval_seconds=3600)
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db, rss=FakeEmptyRSS(), macro=FlakyMacro(), settings=settings, clock=lambda: 0.0
        )

        pipeline.run_once()

        rows = db.scalars(select(CollectorError)).all()
        assert len(rows) == 5  # 每个失败的 FRED 序列一条
        assert {row.collector for row in rows} == {"fred"}
        assert {row.occurrences for row in rows} == {1}


def test_collector_errors_survive_the_session(tmp_path):
    """错误必须真的提交。

    生产会话是 autoflush=False，同一 Session 内读得到不代表已落库；用另一个
    Session（只读已提交数据）验证，否则错误会推迟一轮、进程退出还会丢。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'errors_committed.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(fred_monthly_interval_seconds=86400, fred_daily_interval_seconds=3600)

    with Session(engine, autoflush=False) as db:
        DocumentPipeline(
            db=db, rss=FakeEmptyRSS(), macro=FlakyMacro(), settings=settings, clock=lambda: 0.0
        ).run_once()

    with Session(engine) as fresh:
        assert len(fresh.scalars(select(CollectorError)).all()) == 5


def test_repeated_collector_errors_are_counted_not_duplicated(tmp_path):
    """同一条错误重复出现要累加计数，不能每轮写一行把表撑爆。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'errors_count.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(fred_monthly_interval_seconds=86400, fred_daily_interval_seconds=3600)
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db, rss=FakeEmptyRSS(), macro=FlakyMacro(), settings=settings, clock=lambda: 0.0
        )

        pipeline.run_once()
        pipeline.run_once()  # 同样的失败再来一次

        rows = db.scalars(select(CollectorError)).all()
        assert len(rows) == 5
        assert {row.occurrences for row in rows} == {2}
        assert all(row.last_seen_at >= row.first_seen_at for row in rows)


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


class FakeEmptyRSS:
    def collect(self):
        return CollectorResult(
            source="rss",
            items=[
                NewsItem(
                    source="empty-source",
                    canonical_url="https://example.com/empty",
                    title="Empty article",
                    summary="",
                    content_hash="hash-empty",
                    published_at=datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                )
            ],
        )


def test_document_pipeline_marks_empty_content_as_skipped_not_indexed(tmp_path):
    """空内容文档不应标记 INDEXED（Milvus 无向量），也不应调 Ark 生成摘要。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pipeline_empty.db'}")
    Base.metadata.create_all(engine)
    indexer = FakeIndexer()
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeEmptyRSS(),
            macro=FakeMacro(),
            summary_client=FakeSummary(),
            embedder=FakeEmbedder(),
            indexer=indexer,
        )
        result = pipeline.run_once()
        assert result["processed"] == 1  # fed 事件正常索引
        assert result["skipped"] == 1  # 空内容新闻被跳过
        assert result["failed"] == 0
        records = db.scalars(select(SourceDocument)).all()
        assert len(records) == 2
        empty = next(record for record in records if record.source == "empty-source")
        assert empty.processing_status == "SKIPPED"
        summaries = db.scalars(select(DocumentSummary)).all()
        assert len(summaries) == 1  # 只有 fed 事件有摘要
        assert all(not chunk.document_id.startswith(empty.id) for chunk in indexer.chunks)


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

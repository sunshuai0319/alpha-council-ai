from dataclasses import replace
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


class FreeFormDirectionSummary(FakeSummary):
    """模型返回自由文本方向 —— 线上真实崩过的那个值。

    ``Summary`` 是 pydantic 模型，不是 dataclass —— 用 ``model_copy``
    而不是 ``dataclasses.replace``（后者会抛 TypeError）。
    """

    def summarize(self, document):
        return super().summarize(document).model_copy(update={"direction": "neutral/uncertain"})


class FakeEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeIndexer:
    def __init__(self):
        self.chunks = []

    def insert(self, chunks):
        self.chunks.extend(chunks)


class MultiAssetRSS(FakeRSS):
    def collect(self):
        result = super().collect()
        item = result.items[0]
        result.items[0] = replace(
            item,
            summary="Bitcoin and Ethereum ETF flows increased.",
            title="Bitcoin and Ethereum ETF update",
        )
        return result


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


def test_document_pipeline_stores_a_bounded_direction(tmp_path):
    """模型返回自由文本方向时必须先收敛再落库。

    线上实测：模型回 ``neutral/uncertain``（17 字符），而
    ``document_summaries.direction`` 是 VARCHAR(16) —— 抛
    StringDataRightTruncation 并让**整批**文档回滚（流水线每批只 commit
    一次，flush 会把所有待写行一起刷）。

    ⚠️ 断言落在存下来的值上，而不是「有没有抛异常」：SQLite **不强制**
    VARCHAR 长度，所以这条测试在 SQLite 上永远不会自然复现截断 —— 只断言
    「没炸」的话，归一化被删掉它照样绿。
    """

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'direction.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=FreeFormDirectionSummary(),
            embedder=FakeEmbedder(),
            indexer=FakeIndexer(),
        )
        result = pipeline.run_once()

        assert result["processed"] == 2
        stored = {row.direction for row in db.scalars(select(DocumentSummary)).all()}
        assert stored == {"NEUTRAL"}
        for direction in stored:
            assert len(direction) <= 16


class PoisonTitleSummary(FakeSummary):
    """对某篇标题的文档必失败 —— 用来验证一篇坏文档不会连累整批。"""

    def summarize(self, document):
        if document.title == "Bitcoin ETF update":
            raise RuntimeError("poisoned summary")
        return super().summarize(document)


class PoisonColumnSummary(FakeSummary):
    """让写库本身被数据库拒绝 —— 复现线上那个 bug 的**形状**。

    线上的触发方式是 ``direction`` 超出 VARCHAR(16)；SQLite 不强制 VARCHAR
    长度，所以这里换成 NOT NULL 违例（SQLite 强制执行）。区别只是拒绝的原因，
    关键是**失败发生在 flush 而不是在应用代码里** —— 那正是旧代码漏掉的地方：
    ``flush()`` 的位置在 try 之外，异常一路冒到 ``run_once``，整批回滚。
    """

    def summarize(self, document):
        summary = super().summarize(document)
        if document.title == "Bitcoin ETF update":
            # pydantic v2 默认不在赋值时校验，所以这里能绕过 Field(ge=0, le=1)
            summary.confidence = None
        return summary


class FailingIndexer:
    def insert(self, chunks):
        raise RuntimeError("milvus unreachable")


def test_one_failing_document_does_not_lose_the_rest_of_the_batch(tmp_path):
    """一篇坏文档只该毁掉它自己。

    原先整批只 commit 一次，而 ``flush()`` 会刷整个会话的待写行 —— 实测一篇
    文档的 direction 超出列宽就让当天其后**所有**新文档回滚，流水线停了近
    5 小时（09:29 之后再没写过任何东西）。

    ⚠️ 毒药必须是**数据库层**的拒绝，不能是应用层异常：后者本来就被
    ``_process`` 的 except 捕获，测不出这个 bug。
    """

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'isolation.db'}")
    Base.metadata.create_all(engine)
    indexer = FakeIndexer()
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=PoisonColumnSummary(),
            embedder=FakeEmbedder(),
            indexer=indexer,
        )
        result = pipeline.run_once()

        assert result["failed"] == 1
        assert result["processed"] == 1
        indexed = {
            row.title
            for row in db.scalars(
                select(SourceDocument).where(SourceDocument.processing_status == "INDEXED")
            )
        }
        assert indexed == {"Fed statement"}
        assert indexer.chunks  # 好的那篇确实进了向量库


def test_a_rejected_write_leaves_a_failed_record_behind(tmp_path):
    """被数据库拒绝的文档要留下 FAILED 行，否则下一轮无从知道它试过、试了几次。

    父行必须**先独立落库**才做得到这件事 —— 否则它跟着失败的事务一起回滚，
    于是既没有失败记录、attempts 也不增长，看起来就像从没处理过。
    """

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'rejected.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=PoisonColumnSummary(),
            embedder=FakeEmbedder(),
            indexer=FakeIndexer(),
        )
        pipeline.run_once()

        row = db.scalar(
            select(SourceDocument).where(SourceDocument.title == "Bitcoin ETF update")
        )
        assert row is not None
        assert row.processing_status == "FAILED"
        assert row.processing_attempts == 1
        assert "constraint" in (row.processing_error or "").lower()


def test_indexing_failure_marks_the_document_failed_not_indexed(tmp_path):
    """Postgres 落定之后 Milvus 失败 —— 不能留「已索引但无向量」的假状态。

    这也是为什么写库要排在写向量**之前**：Postgres 是真相来源，Milvus 能从它
    重建（``scripts/reindex_documents.py``）；反过来「向量在、行不在」是孤儿，
    没有自动修复路径。
    """

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'indexing.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=FakeSummary(),
            embedder=FakeEmbedder(),
            indexer=FailingIndexer(),
        )
        result = pipeline.run_once()

        assert result["processed"] == 0
        assert result["failed"] == 2
        statuses = {row.processing_status for row in db.scalars(select(SourceDocument))}
        assert statuses == {"FAILED"}
        # summary 行留下了：它是 Postgres 侧的真相，下一轮重试只需要重写向量
        assert len(db.scalars(select(DocumentSummary)).all()) == 2


def test_documents_stop_being_retried_after_the_attempt_cap(tmp_path):
    """超过上限就不再重试。

    原先没有上限 —— 库里有 attempts=34 的文档，来自一个早已不可达的端点，
    每 5 分钟重试一次，永远失败。而且那个计数原先和坏值同事务，回滚后归零，
    所以连「试了多少次」都记不下来。
    """

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'attempts.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        DOCUMENT_MAX_PROCESSING_ATTEMPTS="2",
    )
    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=PoisonTitleSummary(),
            embedder=FakeEmbedder(),
            indexer=FakeIndexer(),
            settings=settings,
        )
        for _ in range(3):
            pipeline.run_once()

        row = db.scalar(
            select(SourceDocument).where(SourceDocument.title == "Bitcoin ETF update")
        )
        assert row.processing_status == "FAILED"
        assert row.processing_attempts == 2  # 第三轮已放弃，没有第三次尝试


def test_document_pipeline_uses_detected_asset_when_summary_omits_assets(tmp_path):
    """摘要模型漏标资产时，仍使用文档预处理阶段识别出的币种标签。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'asset_fallback.db'}")
    Base.metadata.create_all(engine)
    indexer = FakeIndexer()

    class SummaryWithoutAssets(FakeSummary):
        def summarize(self, document):
            return super().summarize(document).model_copy(update={"assets": []})

    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeRSS(),
            macro=FakeMacro(),
            summary_client=SummaryWithoutAssets(),
            embedder=FakeEmbedder(),
            indexer=indexer,
        )
        pipeline.run_once()

    news_chunk = next(chunk for chunk in indexer.chunks if chunk.source == "test")
    assert news_chunk.asset == "BTC"


def test_document_pipeline_merges_summary_assets_and_indexes_each_asset(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'multi_asset.db'}")
    Base.metadata.create_all(engine)
    indexer = FakeIndexer()

    class SummaryWithExchangeLabels(FakeSummary):
        def summarize(self, document):
            return super().summarize(document).model_copy(
                update={"assets": ["BTC-USDT", "ETH"], "impact_horizon": "short-to-medium"}
            )

    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=MultiAssetRSS(),
            macro=FakeEmptyMacro(),
            summary_client=SummaryWithExchangeLabels(),
            embedder=FakeEmbedder(),
            indexer=indexer,
            settings=Settings(trading_symbols="BTC-USDT,ETH-USDT"),
        )
        pipeline.run_once()

        summary = db.scalar(select(DocumentSummary).where(DocumentSummary.document_id.is_not(None)))
        assert summary is not None
        assert summary.assets == ["BTC", "ETH"]
        assert summary.impact_horizon == "SHORT_MEDIUM"
        assert summary.metadata_json["asset_scope"] == "ASSET_SPECIFIC"

    news_chunks = [chunk for chunk in indexer.chunks if chunk.source == "test"]
    assert {chunk.asset for chunk in news_chunks} == {"BTC", "ETH"}
    assert len(news_chunks) == 2
    assert len({chunk.chunk_id for chunk in news_chunks}) == 2


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


class FakeEmptyMacro:
    def collect(self, series_ids=(), fred_limit=30, fed_feed_url=None):
        del series_ids, fred_limit, fed_feed_url
        return CollectorResult(source="fred"), CollectorResult(source="federal-reserve")


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


class MutableMacro:
    """值可控的 FRED 桩件，用来验证同一观测被重复抓取时会不会更新。

    无视 series_ids 恒返回同一条观测 —— 因此不需要操控 clock 去等调度到期。
    """

    def __init__(self) -> None:
        self.value = 5.1

    def collect(self, series_ids=(), fred_limit=30, fed_feed_url=None):
        del series_ids, fred_limit, fed_feed_url
        return (
            CollectorResult(
                source="fred",
                items=[
                    MacroObservation(
                        series_id="DFF",
                        observation_date=datetime.now(UTC).date(),
                        value=self.value,
                        source_url="https://fred.example/DFF",
                        fetched_at=datetime.now(UTC),
                    )
                ],
                succeeded=["DFF"],
            ),
            CollectorResult(source="federal-reserve"),
        )


def test_republishing_a_macro_observation_updates_the_stored_value(tmp_path):
    """FRED 会修订历史值。落库只 insert 不 update 的话，首次写坏的值永远改不回来。

    这不是假想问题：解析 bug 期间写进去的 152 行全是 NULL，修好解析后仍然会
    因为「行已存在」而全部跳过。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'macro.db'}")
    Base.metadata.create_all(engine)
    macro = MutableMacro()

    with Session(engine) as db:
        pipeline = DocumentPipeline(
            db=db,
            rss=FakeEmptyRSS(),
            macro=macro,
            summary_client=FakeSummary(),
            embedder=FakeEmbedder(),
            indexer=FakeIndexer(),
        )

        pipeline.run_once()
        macro.value = 5.25
        pipeline.run_once()

        # 断言必须在 with 内：出去之后会话关闭，访问属性会 DetachedInstanceError
        rows = db.scalars(select(MacroObservationRecord)).all()
        assert len(rows) == 1, "同一 (series_id, observation_date) 不应重复插入"
        assert rows[0].value == 5.25, "修订后的值必须覆盖旧值"

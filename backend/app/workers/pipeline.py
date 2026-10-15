import logging
import time as time_module
from collections.abc import Callable
from datetime import UTC, datetime, time
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.collectors.macro import DEFAULT_FRED_SERIES, FRED_SERIES_CADENCE, MacroCollector
from app.collectors.rss import RSSCollector
from app.config import Settings, get_settings
from app.db.collector_errors import record_collector_errors
from app.db.models import DocumentSummary as DocumentSummaryRecord
from app.db.models import MacroObservationRecord, SourceDocument
from app.processing.ark import ArkSummaryClient, DocumentSummary
from app.processing.documents import (
    DocumentInput,
    chunk_text,
    classify_asset_scope,
    merge_assets,
    normalize_direction,
    normalize_event_type,
    normalize_impact_horizon,
    prepare_document,
)
from app.rag.embeddings import create_embedder
from app.rag.milvus import MilvusVectorStore
from app.rag.retriever import build_indexed_chunks
from app.workers.schedule import SourceSchedule

logger = logging.getLogger(__name__)


class DocumentPipeline:
    def __init__(
        self,
        *,
        db: Session,
        rss: Any | None = None,
        macro: Any | None = None,
        summary_client: Any | None = None,
        embedder: Any | None = None,
        indexer: Any | None = None,
        settings: Settings | None = None,
        clock: Callable[[], float] = time_module.monotonic,
    ) -> None:
        self.db = db
        self.rss = rss or RSSCollector()
        self.macro = macro or MacroCollector()
        self.settings = settings or get_settings()
        self.summary_client = summary_client or ArkSummaryClient()
        self.embedder = embedder or create_embedder(self.settings)
        self.indexer = indexer or MilvusVectorStore(
            self.settings,
            embedding_dimension=getattr(self.embedder, "dimension", self.settings.doubao_embedding_dimension),
        )
        self._schedule = SourceSchedule(self._fred_intervals(), clock=clock)

    def _fred_intervals(self) -> dict[str, int]:
        monthly = self.settings.fred_monthly_interval_seconds
        daily = self.settings.fred_daily_interval_seconds
        return {
            series: monthly if cadence == "monthly" else daily
            for series, cadence in FRED_SERIES_CADENCE.items()
        }

    def run_once(self) -> dict[str, int]:
        started = time_module.monotonic()
        news = self.rss.collect()
        due_series = tuple(self._schedule.due_keys(DEFAULT_FRED_SERIES))
        observations, events = self.macro.collect(due_series)
        # 只把抓取成功的序列推进到下一个间隔：失败必须下一轮立刻重试，
        # 否则一次短暂故障会让该序列停摆一整天。
        for series_id in observations.succeeded:
            self._schedule.mark(series_id)
        logger.info(
            "collected: news=%d macro_obs=%d fed_events=%d",
            len(news.items),
            len(observations.items),
            len(events.items),
        )
        new_observations = 0
        updated_observations = 0
        for observation in observations.items:
            observation_date = datetime.combine(
                observation.observation_date,
                time.min,
                tzinfo=UTC,
            )
            existing = self.db.scalar(
                select(MacroObservationRecord).where(
                    MacroObservationRecord.series_id == observation.series_id,
                    MacroObservationRecord.observation_date == observation_date,
                )
            )
            if existing is None:
                self.db.add(
                    MacroObservationRecord(
                        series_id=observation.series_id,
                        observation_date=observation_date,
                        value=observation.value,
                        source_url=observation.source_url,
                        fetched_at=observation.fetched_at,
                    )
                )
                new_observations += 1
            elif existing.value != observation.value:
                # FRED 会修订历史值；解析曾经写坏的 NULL 也靠这条自愈。
                existing.value = observation.value
                existing.fetched_at = observation.fetched_at
                updated_observations += 1
        inputs = [
            DocumentInput(
                url=item.canonical_url,
                title=item.title,
                content=item.summary,
                source=item.source,
                published_at=item.published_at,
            )
            for item in news.items
        ]
        inputs.extend(
            DocumentInput(
                url=item.source_url,
                title=item.title,
                content=item.summary,
                source="federal-reserve",
                published_at=item.published_at,
            )
            for item in events.items
        )
        processed = 0
        skipped = 0
        failed = 0
        for document in inputs:
            outcome = self._process(document)
            if outcome is True:
                processed += 1
            elif outcome is None:
                skipped += 1
            else:
                failed += 1
        collector_errors = [*news.errors, *observations.errors, *events.errors]
        for source in collector_errors:
            logger.warning("collector error: %s", source)
        # 必须在 commit 之前：生产会话是 autoflush=False，放在后面会推迟一轮才落库。
        self._record_errors(news, observations, events)
        self.db.commit()
        logger.info(
            "collection: news=%d macro_obs=%d(+%d new/%d updated) fed_events=%d | docs processed=%d skipped=%d failed=%d | collector_errors=%d | %.2fs",
            len(news.items),
            len(observations.items),
            new_observations,
            updated_observations,
            len(events.items),
            processed,
            skipped,
            failed,
            len(collector_errors),
            time_module.monotonic() - started,
        )
        return {
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "collector_errors": len(collector_errors),
        }

    def _record_errors(self, *results: Any) -> None:
        """把采集错误按 (collector, message) 归并落库（调用方负责 commit）。"""

        record_collector_errors(self.db, *results)

    def _process(self, document: DocumentInput) -> bool | None:
        """Return True when indexed, None when already indexed, False on failure.

        **每个文档一个独立事务，且 Postgres 先落定、Milvus 后写。** 这两条都是
        踩过之后才补上的：

        - 原先整批只 commit 一次，而 ``flush()`` 会刷**整个会话**的待写行，
          所以一篇文档的坏值（实测 ``direction`` 超出 VARCHAR(16)）会让整批
          回滚 —— 连着几小时的新文档全都进了又没了。
        - 回滚撤不掉已经写进 Milvus 的向量，那些成了没有对应 Postgres 行的
          孤儿，重试还会再写一份。方向必须对：**Postgres 是真相来源，Milvus
          可以从它重建**（``scripts/reindex_documents.py``）；反过来没有修复路径。
        - ``processing_attempts`` 原先和坏值同事务，回滚后计数归零 → 无限重试。
          现在它随父行先落定，才有可能设上限。
        """

        prepared = prepare_document(document, known_assets=self.settings.asset_list)
        record = self.db.scalar(
            select(SourceDocument).where(
                or_(
                    SourceDocument.canonical_url == prepared.canonical_url,
                    SourceDocument.content_hash == prepared.content_hash,
                )
            )
        )
        if record is not None and record.processing_status == "INDEXED":
            return None
        if record is not None and self._attempts_exhausted(record):
            return None
        if record is None:
            record = SourceDocument(
                id=prepared.document_id,
                source=prepared.source,
                canonical_url=prepared.canonical_url,
                title=prepared.title,
                raw_text=prepared.raw_text,
                cleaned_text=prepared.cleaned_text,
                content_hash=prepared.content_hash,
                language=prepared.language,
                published_at=prepared.published_at,
            )
            self.db.add(record)
        # 用 id 而不是 record 对象往下走：commit / rollback 之后 ORM 对象会过期，
        # 而字符串不会。
        document_id = record.id
        source = record.source
        record.processing_attempts = (record.processing_attempts or 0) + 1
        if not prepared.cleaned_text:
            # 空内容文档没有可分块可向量化的正文，标记为 SKIPPED 而不是 INDEXED，
            # 避免出现「已索引但 Milvus 无向量」的假状态，也不再重复调 Ark。
            self.db.commit()
            self._mark(document_id, "SKIPPED", None)
            return None
        # 父行先独立落库：后面任一步失败都要能找回它标 FAILED。整行回滚掉的话，
        # 既没有失败记录、attempts 也不增长 —— 那正是「无限重试且查不出原因」。
        self.db.commit()

        try:
            summary: DocumentSummary = self.summary_client.summarize(prepared)
            assets = merge_assets(prepared.assets, summary.assets, self.settings.asset_list)
            event_type = normalize_event_type(summary.event_type or prepared.event_type)
            impact_horizon = normalize_impact_horizon(summary.impact_horizon)
            direction = normalize_direction(summary.direction)
            summary = summary.model_copy(
                update={
                    "assets": list(assets),
                    "event_type": event_type,
                    "impact_horizon": impact_horizon,
                    "direction": direction,
                }
            )
            existing_summary = self.db.scalar(
                select(DocumentSummaryRecord).where(DocumentSummaryRecord.document_id == document_id)
            )
            summary_row = existing_summary or DocumentSummaryRecord(
                id=str(uuid4()),
                document_id=document_id,
            )
            summary_row.summary = summary.summary
            summary_row.event_type = summary.event_type
            summary_row.assets = list(assets)
            summary_row.direction = summary.direction
            summary_row.impact_horizon = summary.impact_horizon
            summary_row.confidence = summary.confidence
            summary_row.model_version = "ark:" + getattr(self.summary_client, "model", "configured")
            metadata = dict(summary_row.metadata_json or {})
            metadata.update(
                {
                    "asset_scope": classify_asset_scope(assets, source, event_type),
                    "rag_schema_version": "v2",
                }
            )
            summary_row.metadata_json = metadata
            if existing_summary is None:
                self.db.add(summary_row)
            chunks = chunk_text(prepared.cleaned_text)
            vectors = self.embedder.embed(chunks)
            # Postgres 先落定，Milvus 再写（见 docstring 的方向说明）
            self.db.commit()
        except Exception as exc:
            logger.exception("document summary failed: %s", document_id)
            self.db.rollback()
            self._mark(document_id, "FAILED", str(exc))
            return False

        try:
            self.indexer.insert(
                build_indexed_chunks(
                    prepared,
                    summary,
                    vectors,
                    getattr(self.embedder, "model_name", "BAAI/bge-m3"),
                    texts=chunks,
                )
            )
        except Exception as exc:
            # 退回 FAILED 让下一轮重试 —— 不能留「Postgres 说 INDEXED 但 Milvus
            # 没有向量」的假状态，那正是 SKIPPED 分支刻意避开的东西。
            logger.exception("document indexing failed: %s", document_id)
            self._mark(document_id, "FAILED", str(exc))
            return False

        self._mark(document_id, "INDEXED", None)
        return True

    def _attempts_exhausted(self, record: SourceDocument) -> bool:
        """超过上限就不再重试。

        原先没有上限 —— 库里有 attempts=34 的文档，来自一个早已不可达的端点，
        每 5 分钟重试一次，永远失败。``<= 0`` 表示不限次。
        """

        cap = self.settings.document_max_processing_attempts
        return cap > 0 and (record.processing_attempts or 0) >= cap

    def _mark(self, document_id: str, status: str, error: str | None) -> None:
        """在**自己的事务**里写状态。

        失败路径不能复用出错的那个事务：它已经不可用，在它里面写什么也留不下。
        """

        row = self.db.get(SourceDocument, document_id)
        if row is None:
            logger.warning("cannot mark unknown document %s as %s", document_id, status)
            return
        row.processing_status = status
        row.processing_error = error
        self.db.commit()

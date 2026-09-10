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
from app.db.models import CollectorError, MacroObservationRecord, SourceDocument
from app.db.models import DocumentSummary as DocumentSummaryRecord
from app.processing.ark import ArkSummaryClient, DocumentSummary
from app.processing.documents import DocumentInput, chunk_text, prepare_document
from app.rag.embeddings import BGEEmbedder
from app.rag.milvus import IndexedChunk, MilvusVectorStore
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
        self.summary_client = summary_client or ArkSummaryClient()
        self.embedder = embedder or BGEEmbedder()
        self.indexer = indexer or MilvusVectorStore()
        self.settings = settings or get_settings()
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
            "collection: news=%d macro_obs=%d(+%d new) fed_events=%d | docs processed=%d skipped=%d failed=%d | collector_errors=%d | %.2fs",
            len(news.items),
            len(observations.items),
            new_observations,
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
        """把采集错误按 (collector, message) 归并落库。

        只打日志的话，"网络到底稳不稳"无从判断 —— 翻日志数不出错误率，也分不清
        是单个站点的波动还是整条出口的故障。归并计数避免每轮写一行撑爆表。
        """

        now = datetime.now(UTC)
        for result in results:
            for message in result.errors:
                row = self.db.scalar(
                    select(CollectorError).where(
                        CollectorError.collector == result.source,
                        CollectorError.message == message,
                    )
                )
                if row is None:
                    self.db.add(
                        CollectorError(
                            collector=result.source,
                            message=message,
                            occurrences=1,
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                else:
                    row.occurrences += 1
                    row.last_seen_at = now

    def _process(self, document: DocumentInput) -> bool | None:
        """Return True when indexed, None when already indexed, False on failure."""
        prepared = prepare_document(document)
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
            # autoflush=False 的会话不会在查询时自动写入；先 flush 让父记录落库，
            # 既保证 document_summaries 外键可解析，也让后续去重能查到本批次记录。
            self.db.flush()
        if not prepared.cleaned_text:
            # 空内容文档没有可分块可向量化的正文，标记为 SKIPPED 而不是 INDEXED，
            # 避免出现「已索引但 Milvus 无向量」的假状态，也不再重复调 Ark。
            record.processing_status = "SKIPPED"
            record.processing_error = None
            return None
        record.processing_attempts = (record.processing_attempts or 0) + 1
        try:
            summary: DocumentSummary = self.summary_client.summarize(prepared)
            existing_summary = self.db.scalar(
                select(DocumentSummaryRecord).where(DocumentSummaryRecord.document_id == record.id)
            )
            summary_row = existing_summary or DocumentSummaryRecord(
                id=str(uuid4()),
                document_id=record.id,
            )
            summary_row.summary = summary.summary
            summary_row.event_type = summary.event_type
            summary_row.assets = summary.assets
            summary_row.direction = summary.direction
            summary_row.impact_horizon = summary.impact_horizon
            summary_row.confidence = summary.confidence
            summary_row.model_version = "ark:" + getattr(self.summary_client, "model", "configured")
            if existing_summary is None:
                self.db.add(summary_row)
            chunks = chunk_text(prepared.cleaned_text)
            vectors = self.embedder.embed(chunks)
            self.indexer.insert(
                [
                    IndexedChunk(
                        chunk_id=f"{record.id}:{index}",
                        document_id=record.id,
                        content=chunk,
                        canonical_url=record.canonical_url,
                        source=record.source,
                        asset=summary.assets[0] if summary.assets else "",
                        event_type=summary.event_type,
                        impact_horizon=summary.impact_horizon,
                        published_at=record.published_at,
                        content_hash=record.content_hash,
                        embedding_model="BAAI/bge-m3",
                        embedding=vector,
                    )
                    for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                ]
            )
            record.processing_status = "INDEXED"
            record.processing_error = None
            return True
        except Exception as exc:  # noqa: BLE001 - retain failed state for the next run
            record.processing_status = "FAILED"
            record.processing_error = str(exc)
            return False

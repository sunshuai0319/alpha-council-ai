from datetime import UTC, datetime, time
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.macro import MacroCollector
from app.collectors.rss import RSSCollector
from app.db.models import DocumentSummary as DocumentSummaryRecord
from app.db.models import MacroObservationRecord, SourceDocument
from app.processing.ark import ArkSummaryClient, DocumentSummary
from app.processing.documents import DocumentInput, chunk_text, prepare_document
from app.rag.embeddings import BGEEmbedder
from app.rag.milvus import IndexedChunk, MilvusVectorStore


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
    ) -> None:
        self.db = db
        self.rss = rss or RSSCollector()
        self.macro = macro or MacroCollector()
        self.summary_client = summary_client or ArkSummaryClient()
        self.embedder = embedder or BGEEmbedder()
        self.indexer = indexer or MilvusVectorStore()

    def run_once(self) -> dict[str, int]:
        news = self.rss.collect()
        observations, events = self.macro.collect()
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
        failed = 0
        for document in inputs:
            if self._process(document):
                processed += 1
            else:
                failed += 1
        self.db.commit()
        return {"processed": processed, "failed": failed, "collector_errors": len(news.errors) + len(observations.errors) + len(events.errors)}

    def _process(self, document: DocumentInput) -> bool:
        prepared = prepare_document(document)
        record = self.db.scalar(
            select(SourceDocument).where(SourceDocument.canonical_url == prepared.canonical_url)
        )
        if record is not None and record.processing_status == "INDEXED":
            return False
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

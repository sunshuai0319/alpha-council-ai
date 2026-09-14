"""Re-embed PostgreSQL documents into a new, compatible Milvus RAG collection.

The source PostgreSQL rows and the current Milvus collection are read only.
The target collection is never dropped or cleared; use a new target name for
every schema migration. Re-running against a partially populated target is
supported when the client exposes Milvus ``upsert``.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.models import DocumentSummary as DocumentSummaryRecord
from app.db.models import SourceDocument
from app.db.session import SessionLocal
from app.processing.ark import DocumentSummary
from app.processing.documents import DocumentInput, prepare_document
from app.rag.embeddings import BGEEmbedder
from app.rag.milvus import MilvusVectorStore
from app.rag.retriever import DocumentIndexer

logger = logging.getLogger("reindex_documents")


@dataclass
class ReindexStats:
    selected: int = 0
    indexed: int = 0
    rows: int = 0
    failed: int = 0


def default_target_collection(source_collection: str) -> str:
    """Return a non-destructive v2 name for the current collection."""

    if re.search(r"_v1$", source_collection):
        return re.sub(r"_v1$", "_v2", source_collection)
    return f"{source_collection}_v2"


def target_settings(settings: Settings, collection: str) -> Settings:
    updates = {
        "milvus_collection": collection,
        "milvus_schema_version": "v2",
    }
    if settings.use_zilliz:
        updates["zilliz_collection"] = collection
    return settings.model_copy(update=updates)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-collection", help="New collection name; defaults to the current name with _v2")
    parser.add_argument("--limit", type=int, default=0, help="Maximum documents to process; 0 means all")
    parser.add_argument("--dry-run", action="store_true", help="Only report the number of eligible documents")
    return parser


def run(*, settings: Settings, target_collection: str, limit: int = 0, dry_run: bool = False) -> ReindexStats:
    if limit < 0:
        raise ValueError("limit must not be negative")
    if target_collection == settings.vector_store_collection:
        raise ValueError("target collection must differ from the current collection")

    with SessionLocal() as db:
        statement = (
            select(SourceDocument, DocumentSummaryRecord)
            .join(DocumentSummaryRecord, DocumentSummaryRecord.document_id == SourceDocument.id)
            .where(
                SourceDocument.processing_status == "INDEXED",
                SourceDocument.cleaned_text.is_not(None),
                SourceDocument.cleaned_text != "",
            )
            .order_by(SourceDocument.created_at.asc())
        )
        if limit:
            statement = statement.limit(limit)
        records = db.execute(statement).all()

    stats = ReindexStats(selected=len(records))
    if dry_run:
        logger.info(
            "reindex dry run: source_collection=%s target_collection=%s selected=%d",
            settings.vector_store_collection,
            target_collection,
            stats.selected,
        )
        return stats

    destination_settings = target_settings(settings, target_collection)
    store = MilvusVectorStore(destination_settings, schema_version="v2")
    store.ensure_collection()
    indexer = DocumentIndexer(
        store,
        BGEEmbedder(destination_settings),
        embedding_model="bge-m3",
        write_method="upsert",
    )
    for source, stored_summary in records:
        try:
            content = source.cleaned_text or source.raw_text or ""
            document = prepare_document(
                DocumentInput(
                    url=source.canonical_url,
                    title=source.title,
                    content=content,
                    source=source.source,
                    published_at=source.published_at,
                ),
                document_id=source.id,
                known_assets=destination_settings.asset_list,
            )
            summary = DocumentSummary(
                summary=stored_summary.summary or source.title or "Stored document summary",
                event_type=stored_summary.event_type,
                assets=list(stored_summary.assets or []),
                direction=stored_summary.direction,
                impact_horizon=stored_summary.impact_horizon,
                confidence=stored_summary.confidence,
            )
            chunks = indexer.index(document, summary)
            stats.indexed += 1
            stats.rows += len(chunks)
        except Exception:
            stats.failed += 1
            logger.exception("reindex failed: document_id=%s", source.id)
    logger.info(
        "reindex complete: target_collection=%s selected=%d indexed=%d rows=%d failed=%d",
        target_collection,
        stats.selected,
        stats.indexed,
        stats.rows,
        stats.failed,
    )
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parser().parse_args()
    settings = get_settings()
    target_collection = args.target_collection or default_target_collection(settings.vector_store_collection)
    stats = run(
        settings=settings,
        target_collection=target_collection,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(
        f"selected={stats.selected} indexed={stats.indexed} rows={stats.rows} failed={stats.failed} "
        f"target_collection={target_collection}"
    )


if __name__ == "__main__":
    main()

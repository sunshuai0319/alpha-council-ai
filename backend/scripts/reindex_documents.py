"""Re-embed PostgreSQL documents into the Doubao Milvus RAG collection.

The source PostgreSQL rows and the current Milvus collection are read only.
The target collection is never dropped or cleared; use a new target name for
every schema migration. Re-running against a partially populated target is
supported when the client exposes Milvus ``upsert``.

向量由 ``EMBEDDING_PROVIDER`` 选定的 provider 生成；本脚本把目标固定为 Doubao
集合（v2 schema，1024 维），因此切换 provider 不需要动旧集合。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import DocumentSummary as DocumentSummaryRecord
from app.db.models import SourceDocument
from app.db.session import SessionLocal
from app.processing.ark import DocumentSummary
from app.processing.documents import DocumentInput, prepare_document
from app.rag.embeddings import create_embedder
from app.rag.milvus import MilvusVectorStore
from app.rag.retriever import DocumentIndexer

logger = logging.getLogger("reindex_documents")

#: 回填目标固定是 Doubao 集合；旧 BGE 集合保持只读，env 可随时切回去。
_DOUBAO_COLLECTION_SUFFIX = "doubao_vision_v1"
#: 本地 BGE 已移除，向量只能由 Doubao 生成。dry-run 里如实报出来。
EMBEDDING_PROVIDER = "doubao"
#: 规范集合名形如 `<base>_<provider>_v<N>`，只替换 provider 与版本这一段。
_PROVIDER_VERSION_SUFFIX = re.compile(r"_(?:bge|bge_m3|doubao_vision)_v\d+$")


@dataclass
class ReindexStats:
    selected: int = 0
    indexed: int = 0
    rows: int = 0
    failed: int = 0


def default_target_collection(source_collection: str) -> str:
    """Return the Doubao collection name derived from the source collection.

    源集合名只用来取名字，函数本身不读不写任何集合。规范名
    （`<base>_bge_m3_v2`）替换掉 provider/版本后缀，裸名字追加 `_doubao_v1`。
    """

    base = _PROVIDER_VERSION_SUFFIX.sub("", source_collection)
    if base != source_collection:
        return f"{base}_{_DOUBAO_COLLECTION_SUFFIX}"
    return f"{source_collection}_doubao_v1"


def target_settings(settings: Settings, collection: str) -> Settings:
    """Destination settings for the Doubao collection; the source stays untouched."""

    updates = {
        "zilliz_collection": collection,
        "milvus_schema_version": "v2",
    }
    return settings.model_copy(update=updates)


def dry_run_plan(settings: Settings, target_collection: str, selected: int) -> dict[str, object]:
    """Describe what a reindex would write, without touching Milvus."""

    destination = target_settings(settings, target_collection)
    return {
        "source_collection": settings.zilliz_collection,
        "target_collection": target_collection,
        "provider": EMBEDDING_PROVIDER,
        "dimension": destination.doubao_embedding_dimension,
        "selected": selected,
    }


def format_plan(plan: dict[str, object]) -> str:
    return " ".join(f"{key}={value}" for key, value in plan.items())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-collection",
        help="New collection name; defaults to the Doubao collection name",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum documents to process; 0 means all")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report source/target collection, provider, dimension and document count only",
    )
    return parser


def run(
    *,
    settings: Settings,
    target_collection: str,
    limit: int = 0,
    dry_run: bool = False,
    session_factory: Callable[[], Session] = SessionLocal,
) -> ReindexStats:
    if limit < 0:
        raise ValueError("limit must not be negative")
    # dry-run 只读，不碰任何集合：回填完成、env 已切到新集合之后，默认目标就等于当前
    # 集合，若在这里一并拒绝，诊断命令就永远跑不了了。真正写库时才要求目标不同。
    if not dry_run and target_collection == settings.zilliz_collection:
        raise ValueError("target collection must differ from the current collection")

    destination_settings = target_settings(settings, target_collection)

    with session_factory() as db:
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
            "reindex dry run: %s",
            format_plan(dry_run_plan(settings, target_collection, stats.selected)),
        )
        return stats

    embedder = create_embedder(destination_settings)
    store = MilvusVectorStore(
        destination_settings,
        embedding_dimension=embedder.dimension,
        schema_version="v2",
    )
    store.ensure_collection()
    indexer = DocumentIndexer(
        store,
        embedder,
        embedding_model=embedder.model_name,
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
    target_collection = args.target_collection or default_target_collection(settings.zilliz_collection)
    stats = run(
        settings=settings,
        target_collection=target_collection,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(format_plan(dry_run_plan(settings, target_collection, stats.selected)))
    print(
        f"selected={stats.selected} indexed={stats.indexed} rows={stats.rows} failed={stats.failed} "
        f"target_collection={target_collection}"
    )


if __name__ == "__main__":
    main()

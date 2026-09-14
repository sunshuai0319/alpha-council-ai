import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol

from app.processing.ark import DocumentSummary
from app.processing.documents import (
    ProcessedDocument,
    chunk_text,
    classify_asset_scope,
    merge_assets,
    normalize_event_type,
    normalize_impact_horizon,
)
from app.rag.milvus import IndexedChunk, MilvusVectorStore
from app.rag.query import RetrievalRequest

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> list[float]: ...


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    document_id: str
    content: str
    canonical_url: str
    source: str
    asset: str
    event_type: str
    impact_horizon: str
    vector_score: float
    rerank_score: float
    published_at: datetime | None
    asset_scope: str = "UNKNOWN"


def _hit_entity(hit: Any) -> dict[str, Any]:
    if isinstance(hit, dict):
        entity = hit.get("entity")
        return entity if isinstance(entity, dict) else hit
    return vars(hit)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DocumentIndexer:
    def __init__(
        self,
        vector_store: MilvusVectorStore,
        embedder: Embedder,
        embedding_model: str = "bge-m3",
        write_method: str = "insert",
    ) -> None:
        self.vector_store = vector_store
        self.embedder = embedder
        self.embedding_model = embedding_model
        if write_method not in {"insert", "upsert"}:
            raise ValueError("write_method must be insert or upsert")
        self.write_method = write_method

    def index(self, document: ProcessedDocument, summary: DocumentSummary) -> list[IndexedChunk]:
        texts = chunk_text(document.cleaned_text)
        vectors = self.embedder.embed(texts)
        if len(texts) != len(vectors):
            raise ValueError("embedding count does not match chunk count")
        chunks = build_indexed_chunks(document, summary, vectors, self.embedding_model, texts=texts)
        getattr(self.vector_store, self.write_method)(chunks)
        return chunks


def build_indexed_chunks(
    document: ProcessedDocument,
    summary: DocumentSummary,
    vectors: Sequence[list[float]],
    embedding_model: str,
    *,
    texts: Sequence[str] | None = None,
) -> list[IndexedChunk]:
    """Build the canonical row-expanded representation used by all indexers.

    One text chunk can belong to multiple assets. Milvus v1 has a scalar asset
    field, so rows are expanded per asset rather than storing only the first
    label. A generic row is retained for macro/market-wide evidence.
    """

    chunk_texts = list(texts) if texts is not None else chunk_text(document.cleaned_text)
    if len(chunk_texts) != len(vectors):
        raise ValueError("embedding count does not match chunk count")
    assets = merge_assets(document.assets, summary.assets)
    asset_values = assets or ("",)
    event_type = normalize_event_type(summary.event_type or document.event_type)
    impact_horizon = normalize_impact_horizon(summary.impact_horizon)
    asset_scope = classify_asset_scope(assets, document.source, event_type)
    chunks: list[IndexedChunk] = []
    for index, (text, vector) in enumerate(zip(chunk_texts, vectors, strict=True)):
        for asset in asset_values:
            suffix = "generic" if not asset else re.sub(r"[^A-Z0-9]+", "_", asset)
            chunk_id = f"{document.document_id}:{index}"
            if len(asset_values) > 1:
                chunk_id = f"{chunk_id}:{suffix}"
            chunks.append(
                IndexedChunk(
                    chunk_id=chunk_id,
                    document_id=document.document_id,
                    content=text,
                    canonical_url=document.canonical_url,
                    source=document.source,
                    asset=asset,
                    event_type=event_type,
                    impact_horizon=impact_horizon,
                    published_at=document.published_at,
                    content_hash=document.content_hash,
                    embedding_model=embedding_model,
                    embedding=vector,
                    asset_scope=asset_scope,
                    schema_version="v2",
                )
            )
    return chunks


class Retriever:
    def __init__(self, vector_store: Any, embedder: Embedder, reranker: Reranker) -> None:
        self.vector_store = vector_store
        self.embedder = embedder
        self.reranker = reranker

    @staticmethod
    def _filter_expression(request: RetrievalRequest, asset_filter: str | None) -> str | None:
        filters: list[str] = []
        if asset_filter is not None:
            filters.append(f"asset == '{_escape_filter_value(asset_filter)}'")
        if request.event_type:
            filters.append(f"event_type == '{_escape_filter_value(request.event_type)}'")
        if request.impact_horizon:
            filters.append(f"impact_horizon == '{_escape_filter_value(request.impact_horizon)}'")
        if request.published_after:
            cutoff_ms = int(request.published_after.timestamp() * 1000)
            filters.append(f"published_at >= {cutoff_ms}")
        return " and ".join(filters) or None

    def _search_candidates(
        self,
        request: RetrievalRequest,
        vector: list[float],
        candidate_limit: int,
        asset_filter: str | None,
    ) -> list[tuple[Any, dict[str, Any]]]:
        expression = self._filter_expression(request, asset_filter)
        try:
            raw_hits = self.vector_store.search(vector, candidate_limit, expression)
        except Exception as exc:
            logger.error(
                "rag retrieval search failed: asset_scope=%s error_type=%s",
                self._asset_scope_label(asset_filter),
                type(exc).__name__,
            )
            raise
        candidates: list[tuple[Any, dict[str, Any]]] = []
        for hit in raw_hits:
            entity = _hit_entity(hit)
            if asset_filter is not None and str(entity.get("asset", "")).upper() != asset_filter.upper():
                continue
            if request.event_type and str(entity.get("event_type", "")).upper() != request.event_type:
                continue
            if request.impact_horizon and str(entity.get("impact_horizon", "")).upper() != request.impact_horizon:
                continue
            published_at = _as_datetime(entity.get("published_at"))
            if request.published_after and (published_at is None or published_at < request.published_after):
                continue
            candidates.append((hit, entity))
        logger.info(
            "rag retrieval search: asset_scope=%s raw_hits=%d candidates=%d",
            self._asset_scope_label(asset_filter),
            len(raw_hits),
            len(candidates),
        )
        return candidates

    @staticmethod
    def _asset_scope_label(asset_filter: str | None) -> str:
        if asset_filter == "":
            return "<generic>"
        return asset_filter or "<any>"

    def retrieve(
        self,
        request: RetrievalRequest,
    ) -> list[Evidence]:
        candidate_limit = request.candidate_limit or max(request.limit * 5, 10)
        vector = self.embedder.embed([request.query])[0]
        asset_filters: list[str | None] = [request.asset] if request.asset else [None]
        if request.asset:
            # 空资产标签代表跨币种的通用市场/宏观材料，只作为反向风险上下文。
            asset_filters.append("")
        candidates: list[tuple[Any, dict[str, Any]]] = []
        selected_asset_filter: str | None = None
        for index, asset_filter in enumerate(asset_filters):
            if index > 0 and request.asset:
                logger.info(
                    "rag asset fallback: requested_asset=%s fallback_scope=%s",
                    request.asset,
                    self._asset_scope_label(asset_filter),
                )
            candidates = self._search_candidates(request, vector, candidate_limit, asset_filter)
            if candidates:
                selected_asset_filter = asset_filter
                break
        if not candidates:
            logger.info(
                "rag retrieval complete: requested_asset=%s candidates=0 evidence=0",
                request.asset or "<none>",
            )
            return []
        rerank_started_at = perf_counter()
        logger.info(
            "rag reranker start: asset_scope=%s candidates=%d",
            self._asset_scope_label(selected_asset_filter),
            len(candidates),
        )
        try:
            scores = self.reranker.score(request.query, [entity.get("content", "") for _, entity in candidates])
        except Exception as exc:
            logger.error(
                "rag reranker failed: asset_scope=%s candidates=%d error_type=%s",
                self._asset_scope_label(selected_asset_filter),
                len(candidates),
                type(exc).__name__,
            )
            raise
        logger.info(
            "rag reranker complete: asset_scope=%s candidates=%d results=%d elapsed_ms=%.1f",
            self._asset_scope_label(selected_asset_filter),
            len(candidates),
            len(scores),
            (perf_counter() - rerank_started_at) * 1000,
        )
        if len(scores) != len(candidates):
            raise ValueError("reranker score count does not match candidate count")
        ranked = sorted(
            zip(candidates, scores, strict=True),
            key=lambda item: item[1],
            reverse=True,
        )[: request.limit]
        evidence: list[Evidence] = []
        for (hit, entity), score in ranked:
            published_at = _as_datetime(entity.get("published_at"))
            evidence.append(
                Evidence(
                    chunk_id=str(entity.get("id", hit.get("id", "")) if isinstance(hit, dict) else ""),
                    document_id=str(entity.get("document_id", "")),
                    content=str(entity.get("content", "")),
                    canonical_url=str(entity.get("canonical_url", "")),
                    source=str(entity.get("source", "")),
                    asset=str(entity.get("asset", "")),
                    event_type=str(entity.get("event_type", "")),
                    impact_horizon=str(entity.get("impact_horizon", "")),
                    vector_score=float(str(hit.get("distance", hit.get("score", 0)) or 0)) if isinstance(hit, dict) else 0,
                    rerank_score=float(score),
                    published_at=published_at,
                    asset_scope=str(entity.get("asset_scope", "UNKNOWN")),
                )
            )
        logger.info(
            "rag retrieval complete: requested_asset=%s asset_scope=%s candidates=%d evidence=%d",
            request.asset or "<none>",
            self._asset_scope_label(selected_asset_filter),
            len(candidates),
            len(evidence),
        )
        return evidence

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.processing.ark import DocumentSummary
from app.processing.documents import ProcessedDocument, chunk_text
from app.rag.milvus import IndexedChunk, MilvusVectorStore
from app.rag.query import RetrievalRequest


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
    def __init__(self, vector_store: MilvusVectorStore, embedder: Embedder, embedding_model: str = "bge-m3") -> None:
        self.vector_store = vector_store
        self.embedder = embedder
        self.embedding_model = embedding_model

    def index(self, document: ProcessedDocument, summary: DocumentSummary) -> list[IndexedChunk]:
        texts = chunk_text(document.cleaned_text)
        vectors = self.embedder.embed(texts)
        if len(texts) != len(vectors):
            raise ValueError("embedding count does not match chunk count")
        primary_asset = summary.assets[0] if summary.assets else (document.assets[0] if document.assets else "")
        chunks = [
            IndexedChunk(
                chunk_id=f"{document.document_id}:{index}",
                document_id=document.document_id,
                content=text,
                canonical_url=document.canonical_url,
                source=document.source,
                asset=primary_asset,
                event_type=summary.event_type or document.event_type,
                impact_horizon=summary.impact_horizon,
                published_at=document.published_at,
                content_hash=document.content_hash,
                embedding_model=self.embedding_model,
                embedding=vector,
            )
            for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ]
        self.vector_store.insert(chunks)
        return chunks


class Retriever:
    def __init__(self, vector_store: Any, embedder: Embedder, reranker: Reranker) -> None:
        self.vector_store = vector_store
        self.embedder = embedder
        self.reranker = reranker

    def retrieve(
        self,
        request: RetrievalRequest,
    ) -> list[Evidence]:
        candidate_limit = request.candidate_limit or max(request.limit * 5, 10)
        filters: list[str] = []
        if request.asset:
            filters.append(f"asset == '{_escape_filter_value(request.asset)}'")
        if request.event_type:
            filters.append(f"event_type == '{_escape_filter_value(request.event_type)}'")
        if request.impact_horizon:
            filters.append(f"impact_horizon == '{_escape_filter_value(request.impact_horizon)}'")
        if request.published_after:
            cutoff_ms = int(request.published_after.timestamp() * 1000)
            filters.append(f"published_at >= {cutoff_ms}")
        expression = " and ".join(filters) or None
        vector = self.embedder.embed([request.query])[0]
        raw_hits = self.vector_store.search(vector, candidate_limit, expression)
        candidates: list[tuple[Any, dict[str, Any]]] = []
        for hit in raw_hits:
            entity = _hit_entity(hit)
            if request.asset and str(entity.get("asset", "")).upper() != request.asset:
                continue
            if request.event_type and str(entity.get("event_type", "")).upper() != request.event_type:
                continue
            if request.impact_horizon and str(entity.get("impact_horizon", "")).upper() != request.impact_horizon:
                continue
            published_at = _as_datetime(entity.get("published_at"))
            if request.published_after and (published_at is None or published_at < request.published_after):
                continue
            candidates.append((hit, entity))
        if not candidates:
            return []
        scores = self.reranker.score(request.query, [entity.get("content", "") for _, entity in candidates])
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
                )
            )
        return evidence

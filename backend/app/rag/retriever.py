from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.processing.ark import DocumentSummary
from app.processing.documents import ProcessedDocument, chunk_text
from app.rag.milvus import IndexedChunk, MilvusVectorStore


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
        query: str,
        *,
        asset: str | None = None,
        event_type: str | None = None,
        limit: int = 3,
        candidate_limit: int | None = None,
    ) -> list[Evidence]:
        if limit < 1:
            raise ValueError("retrieval limit must be positive")
        candidate_limit = candidate_limit or max(limit * 5, 10)
        filters: list[str] = []
        if asset:
            filters.append(f"asset == '{asset.upper()}'")
        if event_type:
            filters.append(f"event_type == '{event_type.upper()}'")
        expression = " and ".join(filters) or None
        vector = self.embedder.embed([query])[0]
        raw_hits = self.vector_store.search(vector, candidate_limit, expression)
        candidates: list[tuple[Any, dict[str, Any]]] = []
        for hit in raw_hits:
            entity = _hit_entity(hit)
            if asset and str(entity.get("asset", "")).upper() != asset.upper():
                continue
            if event_type and str(entity.get("event_type", "")).upper() != event_type.upper():
                continue
            candidates.append((hit, entity))
        if not candidates:
            return []
        scores = self.reranker.score(query, [entity.get("content", "") for _, entity in candidates])
        if len(scores) != len(candidates):
            raise ValueError("reranker score count does not match candidate count")
        ranked = sorted(
            zip(candidates, scores, strict=True),
            key=lambda item: item[1],
            reverse=True,
        )[:limit]
        evidence: list[Evidence] = []
        for (hit, entity), score in ranked:
            published_at = entity.get("published_at")
            if isinstance(published_at, (int, float)) and published_at:
                published_at = datetime.fromtimestamp(published_at / 1000, tz=UTC)
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
                    published_at=published_at if isinstance(published_at, datetime) else None,
                )
            )
        return evidence

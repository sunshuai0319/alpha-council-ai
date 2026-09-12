from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import Settings, get_settings


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    document_id: str
    content: str
    canonical_url: str
    source: str
    asset: str
    event_type: str
    impact_horizon: str
    published_at: datetime | None
    content_hash: str
    embedding_model: str
    embedding: list[float]


class MilvusVectorStore:
    """Small Milvus client wrapper for the configured BGE-M3 collection."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
        embedding_dimension: int = 1024,
    ) -> None:
        self.settings = settings or get_settings()
        self.collection = self.settings.vector_store_collection
        self.embedding_dimension = embedding_dimension
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from pymilvus import MilvusClient

            kwargs: dict[str, Any] = {"uri": self.settings.vector_store_uri}
            if self.settings.vector_store_token:
                kwargs["token"] = self.settings.vector_store_token
            elif self.settings.vector_store_user:
                kwargs["user"] = self.settings.vector_store_user
                kwargs["password"] = self.settings.vector_store_password
            if self.settings.vector_store_db_name:
                kwargs["db_name"] = self.settings.vector_store_db_name
            self._client = MilvusClient(**kwargs)
        return self._client

    def ensure_collection(self) -> None:
        if self.client.has_collection(collection_name=self.collection):
            return
        from pymilvus import DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_fields=False)
        schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=160)
        schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="canonical_url", datatype=DataType.VARCHAR, max_length=2048)
        schema.add_field(field_name="source", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="asset", datatype=DataType.VARCHAR, max_length=32)
        schema.add_field(field_name="event_type", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="impact_horizon", datatype=DataType.VARCHAR, max_length=32)
        schema.add_field(field_name="published_at", datatype=DataType.INT64)
        schema.add_field(field_name="content_hash", datatype=DataType.VARCHAR, max_length=128)
        schema.add_field(field_name="embedding_model", datatype=DataType.VARCHAR, max_length=128)
        schema.add_field(
            field_name="embedding",
            datatype=DataType.FLOAT_VECTOR,
            dim=self.embedding_dimension,
        )
        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(
            collection_name=self.collection,
            schema=schema,
            index_params=index_params,
        )

    def insert(self, chunks: list[IndexedChunk]) -> Any:
        if not chunks:
            return None
        self.ensure_collection()
        rows = [
            {
                "id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "content": chunk.content,
                "canonical_url": chunk.canonical_url,
                "source": chunk.source,
                "asset": chunk.asset,
                "event_type": chunk.event_type,
                "impact_horizon": chunk.impact_horizon,
                "published_at": int(chunk.published_at.timestamp() * 1000) if chunk.published_at else 0,
                "content_hash": chunk.content_hash,
                "embedding_model": chunk.embedding_model,
                "embedding": chunk.embedding,
            }
            for chunk in chunks
        ]
        return self.client.insert(collection_name=self.collection, data=rows)

    def search(self, vector: list[float], limit: int, filter_expression: str | None = None) -> list[dict[str, Any]]:
        self.ensure_collection()
        raw = self.client.search(
            collection_name=self.collection,
            data=[vector],
            limit=limit,
            filter=filter_expression or "",
            output_fields=[
                "document_id",
                "content",
                "canonical_url",
                "source",
                "asset",
                "event_type",
                "impact_horizon",
                "published_at",
                "content_hash",
                "embedding_model",
            ],
        )
        return raw[0] if raw and isinstance(raw[0], list) else raw

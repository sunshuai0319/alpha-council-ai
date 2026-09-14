import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

BASE_FIELDS = (
    "id",
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
    "embedding",
)
V2_FIELDS = ("asset_scope", "schema_version")


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
    asset_scope: str = "UNKNOWN"
    schema_version: str = "v2"


class MilvusVectorStore:
    """Small Milvus client wrapper for the configured BGE-M3 collection."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
        embedding_dimension: int = 1024,
        schema_version: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.collection = self.settings.vector_store_collection
        self.embedding_dimension = embedding_dimension
        self.schema_version = str(
            schema_version or getattr(self.settings, "milvus_schema_version", "v1")
        ).lower()
        self._client = client
        self._collection_ready = False
        self._field_names: set[str] = set(BASE_FIELDS)

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
            logger.info(
                "milvus client initialized: collection=%s mode=%s",
                self.collection,
                "zilliz" if self.settings.use_zilliz else "milvus",
            )
        return self._client

    def ensure_collection(self) -> None:
        exists = self.client.has_collection(collection_name=self.collection)
        if exists:
            self._load_field_names()
            if not self._collection_ready:
                logger.info(
                    "milvus collection ready: collection=%s existing=true schema_version=%s fields=%d",
                    self.collection,
                    self.schema_version,
                    len(self._field_names),
                )
            self._collection_ready = True
            return
        from pymilvus import DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_fields=False)
        schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=160)
        schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="canonical_url", datatype=DataType.VARCHAR, max_length=2048)
        schema.add_field(field_name="source", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="asset", datatype=DataType.VARCHAR, max_length=128)
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
        if self.schema_version == "v2":
            schema.add_field(field_name="asset_scope", datatype=DataType.VARCHAR, max_length=32)
            schema.add_field(field_name="schema_version", datatype=DataType.VARCHAR, max_length=16)
        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(
            collection_name=self.collection,
            schema=schema,
            index_params=index_params,
        )
        self._collection_ready = True
        self._field_names = set(BASE_FIELDS)
        if self.schema_version == "v2":
            self._field_names.update(V2_FIELDS)
        logger.info(
            "milvus collection ready: collection=%s existing=false schema_version=%s fields=%d",
            self.collection,
            self.schema_version,
            len(self._field_names),
        )

    def _load_field_names(self) -> None:
        """Load the real schema so v2 rows remain compatible with a v1 collection."""

        describe = getattr(self.client, "describe_collection", None)
        if not callable(describe):
            self._field_names = set(BASE_FIELDS)
            return
        try:
            description = describe(collection_name=self.collection)
            fields = description.get("fields", []) if isinstance(description, dict) else []
            names = {
                str(field.get("name"))
                for field in fields
                if isinstance(field, dict) and field.get("name")
            }
            self._field_names = names or set(BASE_FIELDS)
        except Exception as exc:  # noqa: BLE001 - compatibility fallback is safer than a hard stop
            self._field_names = set(BASE_FIELDS)
            logger.warning(
                "milvus schema describe failed: collection=%s error_type=%s fallback_fields=%d",
                self.collection,
                type(exc).__name__,
                len(self._field_names),
            )

    @staticmethod
    def _rows_for_chunks(chunks: list[IndexedChunk], field_names: set[str]) -> list[dict[str, Any]]:
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
                "asset_scope": chunk.asset_scope,
                "schema_version": chunk.schema_version,
            }
            for chunk in chunks
        ]
        return [{field: value for field, value in row.items() if field in field_names} for row in rows]

    def _write(self, chunks: list[IndexedChunk], operation: str) -> Any:
        if not chunks:
            return None
        asset_counts = dict(
            sorted(Counter(chunk.asset or "<generic>" for chunk in chunks).items())
        )
        started_at = perf_counter()
        logger.info(
            "milvus %s start: collection=%s rows=%d asset_counts=%s",
            operation,
            self.collection,
            len(chunks),
            asset_counts,
        )
        try:
            self.ensure_collection()
            rows = self._rows_for_chunks(chunks, self._field_names)
            result = getattr(self.client, operation)(collection_name=self.collection, data=rows)
        except Exception as exc:
            logger.error(
                "milvus %s failed: collection=%s rows=%d error_type=%s",
                operation,
                self.collection,
                len(chunks),
                type(exc).__name__,
            )
            raise
        logger.info(
            "milvus %s complete: collection=%s rows=%d asset_counts=%s elapsed_ms=%.1f response_type=%s",
            operation,
            self.collection,
            len(chunks),
            asset_counts,
            (perf_counter() - started_at) * 1000,
            type(result).__name__,
        )
        return result

    def insert(self, chunks: list[IndexedChunk]) -> Any:
        return self._write(chunks, "insert")

    def upsert(self, chunks: list[IndexedChunk]) -> Any:
        """Idempotent write used by backfills; normal ingestion keeps insert semantics."""

        if not callable(getattr(self.client, "upsert", None)):
            logger.warning(
                "milvus upsert unavailable: collection=%s fallback=insert",
                self.collection,
            )
            return self.insert(chunks)
        return self._write(chunks, "upsert")

    def search(self, vector: list[float], limit: int, filter_expression: str | None = None) -> list[dict[str, Any]]:
        started_at = perf_counter()
        logger.info(
            "milvus search start: collection=%s limit=%d vector_dim=%d filter=%s",
            self.collection,
            limit,
            len(vector),
            filter_expression or "<none>",
        )
        try:
            self.ensure_collection()
            output_fields = [field for field in (*BASE_FIELDS[:-1], *V2_FIELDS) if field in self._field_names]
            raw = self.client.search(
                collection_name=self.collection,
                data=[vector],
                limit=limit,
                filter=filter_expression or "",
                output_fields=output_fields,
            )
        except Exception as exc:
            logger.error(
                "milvus search failed: collection=%s limit=%d error_type=%s",
                self.collection,
                limit,
                type(exc).__name__,
            )
            raise
        hits = raw[0] if raw and isinstance(raw[0], list) else raw
        hit_count = len(hits) if isinstance(hits, list) else 0
        logger.info(
            "milvus search complete: collection=%s raw_hits=%d elapsed_ms=%.1f",
            self.collection,
            hit_count,
            (perf_counter() - started_at) * 1000,
        )
        return hits

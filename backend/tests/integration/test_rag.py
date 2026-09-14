import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from app.config import Settings
from app.processing.ark import DocumentSummary
from app.processing.documents import DocumentInput, prepare_document
from app.rag.milvus import IndexedChunk, MilvusVectorStore
from app.rag.query import RetrievalRequest
from app.rag.retriever import DocumentIndexer, Retriever


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class FakeReranker:
    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        del query
        return [float(index) for index, _ in enumerate(documents)]


class FakeMilvus:
    def __init__(self) -> None:
        self.inserted = []
        self.last_filter = None
        self.filters = []
        self.hits = [
            {
                "id": "btc-1",
                "distance": 0.8,
                "entity": {
                    "id": "btc-1",
                    "document_id": "doc-btc",
                    "content": "Bitcoin ETF flows increased.",
                    "canonical_url": "https://example.com/btc",
                    "source": "test",
                    "asset": "BTC",
                    "event_type": "ETF",
                    "impact_horizon": "SHORT",
                    "published_at": 1700000000000,
                },
            },
            {
                "id": "btc-old",
                "distance": 0.7,
                "entity": {
                    "id": "btc-old",
                    "document_id": "doc-btc-old",
                    "content": "Old Bitcoin headline.",
                    "canonical_url": "https://example.com/btc-old",
                    "source": "test",
                    "asset": "BTC",
                    "event_type": "MARKET",
                    "impact_horizon": "SHORT",
                    "published_at": 1699999999000,
                },
            },
            {
                "id": "btc-unknown-date",
                "distance": 0.6,
                "entity": {
                    "id": "btc-unknown-date",
                    "document_id": "doc-btc-unknown-date",
                    "content": "Undated Bitcoin headline.",
                    "canonical_url": "https://example.com/btc-unknown-date",
                    "source": "test",
                    "asset": "BTC",
                    "event_type": "MARKET",
                    "impact_horizon": "SHORT",
                },
            },
            {
                "id": "eth-1",
                "distance": 0.9,
                "entity": {
                    "id": "eth-1",
                    "document_id": "doc-eth",
                    "content": "Ethereum upgrade progress.",
                    "canonical_url": "https://example.com/eth",
                    "source": "test",
                    "asset": "ETH",
                    "event_type": "MARKET",
                    "impact_horizon": "WEEKS",
                },
            },
        ]

    def insert(self, chunks: list[object]) -> None:
        self.inserted.extend(chunks)

    def search(self, vector: list[float], limit: int, filter_expression: str | None = None) -> list[dict]:
        del vector, limit
        self.last_filter = filter_expression
        self.filters.append(filter_expression)
        return self.hits


class AssetFallbackMilvus(FakeMilvus):
    def search(self, vector: list[float], limit: int, filter_expression: str | None = None) -> list[dict]:
        del vector, limit
        self.last_filter = filter_expression
        self.filters.append(filter_expression)
        if filter_expression and "asset == 'BCH'" in filter_expression:
            return []
        return [
            {
                "id": "generic-1",
                "distance": 0.8,
                "entity": {
                    "id": "generic-1",
                    "document_id": "doc-generic",
                    "content": "Crypto market risk increased.",
                    "canonical_url": "https://example.com/generic",
                    "source": "test",
                    "asset": "",
                    "event_type": "MACRO",
                    "impact_horizon": "DAYS",
                    "published_at": 1700000000000,
                },
            }
        ]


class FakeMilvusClient:
    def __init__(self) -> None:
        self.inserted_rows: list[dict] = []
        self.search_calls: list[dict] = []

    def has_collection(self, collection_name: str) -> bool:
        del collection_name
        return True

    def insert(self, collection_name: str, data: list[dict]) -> dict:
        del collection_name
        self.inserted_rows.extend(data)
        return {"insert_count": len(data)}

    def search(
        self,
        collection_name: str,
        data: list[list[float]],
        limit: int,
        filter: str,
        output_fields: list[str],
    ) -> list[list[dict]]:
        self.search_calls.append(
            {
                "collection_name": collection_name,
                "data": data,
                "limit": limit,
                "filter": filter,
                "output_fields": output_fields,
            }
        )
        return [
            [
                {
                    "id": "milvus-1",
                    "distance": 0.91,
                    "entity": {"id": "milvus-1", "asset": "BCH"},
                }
            ]
        ]


class DescribedFakeMilvusClient(FakeMilvusClient):
    def describe_collection(self, collection_name: str) -> dict:
        del collection_name
        return {
            "fields": [
                {"name": field}
                for field in (
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
                    "asset_scope",
                    "schema_version",
                )
            ]
        }


def test_retriever_applies_asset_filter_before_rerank() -> None:
    milvus = FakeMilvus()
    retriever = Retriever(milvus, FakeEmbedder(), FakeReranker())
    results = retriever.retrieve(RetrievalRequest(query="BTC ETF", asset="BTC", limit=3))
    assert results
    assert all(item.asset == "BTC" for item in results)
    assert milvus.last_filter == "asset == 'BTC'"


def test_retrieval_request_normalizes_exchange_asset_and_horizon() -> None:
    request = RetrievalRequest(
        query="BCH downside risk",
        asset="BCHSUSDT",
        impact_horizon="short-to-medium",
    )
    assert request.asset == "BCH"
    assert request.impact_horizon == "SHORT_MEDIUM"


def test_retriever_applies_recent_and_impact_filters_before_rerank() -> None:
    milvus = FakeMilvus()
    retriever = Retriever(milvus, FakeEmbedder(), FakeReranker())
    results = retriever.retrieve(
        RetrievalRequest(
            query="BTC bearish downside risk",
            asset="BTC",
            direction="LONG",
            published_after=datetime.fromtimestamp(1700000000000 / 1000, tz=UTC),
            impact_horizon="days",
            limit=3,
        )
    )

    assert results
    assert all(item.asset == "BTC" for item in results)
    assert all(item.impact_horizon == "SHORT" for item in results)
    assert [item.chunk_id for item in results] == ["btc-1"]
    assert "asset == 'BTC'" in milvus.last_filter
    assert "impact_horizon == 'SHORT'" in milvus.last_filter
    assert "published_at >= 1700000000000" in milvus.last_filter


def test_retriever_falls_back_to_generic_evidence_when_asset_has_no_hits() -> None:
    milvus = AssetFallbackMilvus()
    reranker = FakeReranker()
    retriever = Retriever(milvus, FakeEmbedder(), reranker)

    results = retriever.retrieve(RetrievalRequest(query="BCH bearish risk", asset="BCH", limit=1))

    assert len(results) == 1
    assert results[0].asset == ""
    assert milvus.filters == ["asset == 'BCH'", "asset == ''"]


def test_retriever_logs_asset_fallback_and_reranker_path(caplog) -> None:
    milvus = AssetFallbackMilvus()
    retriever = Retriever(milvus, FakeEmbedder(), FakeReranker())

    with caplog.at_level(logging.INFO, logger="app.rag.retriever"):
        results = retriever.retrieve(RetrievalRequest(query="BCH bearish risk", asset="BCH", limit=1))

    assert results
    messages = [record.getMessage() for record in caplog.records]
    assert any("rag retrieval search" in message for message in messages)
    assert any("rag asset fallback" in message for message in messages)
    assert any("rag reranker start" in message for message in messages)
    assert any("rag reranker complete" in message for message in messages)
    assert all("BCH bearish risk" not in message for message in messages)


def test_milvus_logs_search_and_insert_without_connection_secrets(caplog) -> None:
    settings = Settings(
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        MILVUS_URI="https://milvus.example.test",
        MILVUS_TOKEN="secret-token",
        ARK_API_KEY="test-key",
    )
    client = FakeMilvusClient()
    store = MilvusVectorStore(settings=settings, client=client, embedding_dimension=2)
    chunk = IndexedChunk(
        chunk_id="chunk-1",
        document_id="doc-1",
        content="BCH market update",
        canonical_url="https://example.com/bch",
        source="test",
        asset="BCH",
        event_type="MARKET",
        impact_horizon="DAYS",
        published_at=None,
        content_hash="hash-1",
        embedding_model="bge-m3",
        embedding=[0.1, 0.2],
    )

    with caplog.at_level(logging.INFO, logger="app.rag.milvus"):
        store.search([0.1, 0.2], limit=3, filter_expression="asset == 'BCH'")
        store.insert([chunk])

    messages = [record.getMessage() for record in caplog.records]
    assert any("milvus search start" in message and "asset == 'BCH'" in message for message in messages)
    assert any("milvus search complete" in message and "raw_hits=1" in message for message in messages)
    assert any("milvus insert start" in message and "rows=1" in message for message in messages)
    assert any("milvus insert complete" in message and "asset_counts={'BCH': 1}" in message for message in messages)
    assert all("secret-token" not in message for message in messages)


def test_milvus_v2_persists_asset_scope_and_schema_version() -> None:
    settings = Settings(
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        MILVUS_URI="https://milvus.example.test",
        ARK_API_KEY="test-key",
        milvus_schema_version="v2",
    )
    client = DescribedFakeMilvusClient()
    store = MilvusVectorStore(settings=settings, client=client, embedding_dimension=2)
    chunk = IndexedChunk(
        chunk_id="chunk-v2",
        document_id="doc-v2",
        content="BCH market update",
        canonical_url="https://example.com/bch",
        source="test",
        asset="BCH",
        event_type="MARKET",
        impact_horizon="SHORT",
        published_at=None,
        content_hash="hash-v2",
        embedding_model="bge-m3",
        embedding=[0.1, 0.2],
        asset_scope="ASSET_SPECIFIC",
        schema_version="v2",
    )

    store.insert([chunk])
    store.search([0.1, 0.2], limit=1)

    assert client.inserted_rows[0]["asset_scope"] == "ASSET_SPECIFIC"
    assert client.inserted_rows[0]["schema_version"] == "v2"
    assert "asset_scope" in client.search_calls[0]["output_fields"]


def test_document_indexer_writes_bge_metadata_and_vectors() -> None:
    milvus = FakeMilvus()
    document = prepare_document(
        DocumentInput("https://example.com/a", "BTC news", "Bitcoin ETF flows increased", "test")
    )
    summary = DocumentSummary(
        summary="ETF flows increased.",
        event_type="ETF",
        assets=["BTC"],
        direction="BULLISH",
        impact_horizon="DAYS",
        confidence=0.9,
    )
    chunks = DocumentIndexer(milvus, FakeEmbedder()).index(document, summary)
    assert len(chunks) == 1
    assert milvus.inserted[0].asset == "BTC"
    assert milvus.inserted[0].embedding_model == "bge-m3"


def test_document_indexer_expands_multi_asset_rows() -> None:
    milvus = FakeMilvus()
    document = prepare_document(
        DocumentInput("https://example.com/a", "Market news", "Bitcoin and Ethereum moved", "test")
    )
    summary = DocumentSummary(
        summary="Both assets moved.",
        event_type="MARKET",
        assets=["BTC-USDT", "ETH"],
        direction="NEUTRAL",
        impact_horizon="short",
        confidence=0.8,
    )

    chunks = DocumentIndexer(milvus, FakeEmbedder()).index(document, summary)

    assert len(chunks) == 2
    assert {chunk.asset for chunk in chunks} == {"BTC", "ETH"}
    assert {chunk.asset_scope for chunk in chunks} == {"ASSET_SPECIFIC"}
    assert len({chunk.chunk_id for chunk in chunks}) == 2

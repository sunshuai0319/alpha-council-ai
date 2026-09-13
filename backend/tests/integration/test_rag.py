from collections.abc import Sequence
from datetime import UTC, datetime

from app.processing.ark import DocumentSummary
from app.processing.documents import DocumentInput, prepare_document
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
                    "impact_horizon": "DAYS",
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
                    "impact_horizon": "DAYS",
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
                    "impact_horizon": "DAYS",
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
        return self.hits


def test_retriever_applies_asset_filter_before_rerank() -> None:
    milvus = FakeMilvus()
    retriever = Retriever(milvus, FakeEmbedder(), FakeReranker())
    results = retriever.retrieve(RetrievalRequest(query="BTC ETF", asset="BTC", limit=3))
    assert results
    assert all(item.asset == "BTC" for item in results)
    assert milvus.last_filter == "asset == 'BTC'"


def test_retriever_applies_recent_and_impact_filters_before_rerank() -> None:
    milvus = FakeMilvus()
    retriever = Retriever(milvus, FakeEmbedder(), FakeReranker())
    results = retriever.retrieve(
        RetrievalRequest(
            query="BTC bearish downside risk",
            asset="BTC",
            direction="LONG",
            published_after=datetime.fromtimestamp(1700000000000 / 1000, tz=UTC),
            impact_horizon="DAYS",
            limit=3,
        )
    )

    assert results
    assert all(item.asset == "BTC" for item in results)
    assert all(item.impact_horizon == "DAYS" for item in results)
    assert [item.chunk_id for item in results] == ["btc-1"]
    assert "asset == 'BTC'" in milvus.last_filter
    assert "impact_horizon == 'DAYS'" in milvus.last_filter
    assert "published_at >= 1700000000000" in milvus.last_filter


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

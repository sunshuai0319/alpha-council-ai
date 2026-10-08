import httpx

from app.config import Settings
from app.rag.embeddings import DoubaoEmbedder, create_embedder


def _settings(**overrides) -> Settings:
    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "MILVUS_URI": "http://milvus:19530",
        "ARK_API_KEY": "ark-test-key",
        "EMBEDDING_PROVIDER": "doubao",
        "DOUBAO_API_KEY": "doubao-test-key",
        "DOUBAO_EMBEDDING_MODEL": "doubao-embedding-vision-251215",
        "DOUBAO_EMBEDDING_DIMENSION": 2,
        "DOUBAO_EMBEDDING_BASE_URL": "https://ark.example.test/api/v3",
    }
    values.update(overrides)
    return Settings(**values)


def test_doubao_embedder_posts_text_items_and_preserves_order() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        requests.append({"url": str(request.url), "json": payload})
        data = httpx.Response(200, json={}).json()
        del data
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"total_tokens": 3},
            },
        )

    settings = _settings()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        embedder = DoubaoEmbedder(settings, client=client)
        vectors = embedder.embed(["first"])

    assert vectors == [[0.1, 0.2]]
    assert requests[0]["url"] == "https://ark.example.test/api/v3/embeddings/multimodal"
    assert requests[0]["json"]


def test_create_embedder_uses_configured_provider() -> None:
    settings = _settings()
    assert isinstance(create_embedder(settings), DoubaoEmbedder)


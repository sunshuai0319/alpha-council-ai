import json
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.rag.embeddings import DoubaoEmbedder, create_embedder


def _settings(**overrides) -> Settings:
    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "ZILLIZ_URI": "http://milvus:19530",
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


def _text_of(request: httpx.Request) -> str:
    """每个请求只带一条文本 —— 多条会被接口融合成一个向量。"""

    payload = json.loads(request.read())
    items = payload["input"]
    assert len(items) == 1, f"each request must carry exactly one text item, got {len(items)}"
    return items[0]["text"]


def test_doubao_embedder_embeds_concurrently_and_keeps_input_order() -> None:
    """并发只提速，顺序必须仍然是输入顺序 —— 顺序错了向量就配错 chunk 了。"""

    lock = threading.Lock()
    active = 0
    peak = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        text = _text_of(request)
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return httpx.Response(200, json={"data": {"embedding": [float(text), 1.0]}, "usage": {}})

    settings = _settings(DOUBAO_MAX_CONCURRENCY=4)
    texts = [str(index) for index in range(12)]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        vectors = DoubaoEmbedder(settings, client=client).embed(texts)

    assert [vector[0] for vector in vectors] == [float(text) for text in texts]
    assert peak > 1, "请求应当并发发出"
    assert peak <= 4, "并发数不得超过 DOUBAO_MAX_CONCURRENCY"


def test_doubao_embedder_propagates_failure_instead_of_dropping_vectors() -> None:
    """部分失败必须整体报错：静默少返回向量会让 chunk 与向量错位。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if _text_of(request) == "bad":
            return httpx.Response(400, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"data": {"embedding": [0.0, 1.0]}, "usage": {}})

    settings = _settings()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        embedder = DoubaoEmbedder(settings, client=client)
        with pytest.raises(httpx.HTTPStatusError):
            embedder.embed(["ok", "bad", "also-ok"])


def test_doubao_embedder_sends_no_request_for_empty_input() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_text_of(request))
        return httpx.Response(200, json={"data": {"embedding": [0.0, 1.0]}, "usage": {}})

    settings = _settings()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        embedder = DoubaoEmbedder(settings, client=client)
        assert embedder.embed([]) == []

    assert calls == []



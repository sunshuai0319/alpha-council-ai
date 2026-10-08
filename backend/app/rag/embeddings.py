import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class DoubaoEmbedder:
    """Doubao multimodal embedding adapter for text-only RAG inputs."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client

    @property
    def model_name(self) -> str:
        return self.settings.doubao_embedding_model

    @property
    def dimension(self) -> int:
        return self.settings.doubao_embedding_dimension

    @property
    def endpoint(self) -> str:
        return f"{self.settings.doubao_embedding_base_url.rstrip('/')}/embeddings/multimodal"

    def _request(self, text: str) -> list[float]:
        if not self.settings.doubao_api_key:
            raise RuntimeError("DOUBAO_API_KEY is not configured")
        payload = {
            "model": self.model_name,
            "input": [{"type": "text", "text": text}],
            "encoding_format": "float",
            "dimensions": self.dimension,
            "instructions": self.settings.doubao_embedding_instructions,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.doubao_api_key}",
            "Content-Type": "application/json",
        }
        if self.client is None:
            response = httpx.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.settings.doubao_timeout_seconds,
            )
        else:
            response = self.client.post(self.endpoint, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            embedding = data.get("embedding")
        elif isinstance(data, list) and data:
            embedding = data[0].get("embedding") if isinstance(data[0], dict) else None
        else:
            embedding = None
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError("Doubao embedding response does not contain a vector")
        vector = [float(value) for value in embedding]
        if len(vector) != self.dimension:
            raise RuntimeError(
                f"Doubao embedding dimension mismatch: expected={self.dimension} actual={len(vector)}"
            )
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        logger.info(
            "doubao embedding complete: model=%s dimensions=%d tokens=%s",
            self.model_name,
            len(vector),
            usage.get("total_tokens", "unknown") if isinstance(usage, dict) else "unknown",
        )
        return vector

    @property
    def max_concurrency(self) -> int:
        return max(1, self.settings.doubao_max_concurrency)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """并发嵌入多条文本，返回顺序与输入一致。

        `/embeddings/multimodal` 一次请求只产出一个向量（多条 input 会被融合成一个），
        所以这里只能一条文本一个请求，提速靠并发 —— 官方文档给的也是这个方案。
        `ThreadPoolExecutor.map` 既保序，也会把第一个异常原样抛出，
        不会静默少返回向量（少返回会让 chunk 与向量错位）。
        """

        items = list(texts)
        if not items:
            return []
        workers = min(self.max_concurrency, len(items))
        if workers == 1:
            return [self._request(text) for text in items]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="doubao-embed") as pool:
            return list(pool.map(self._request, items))


def create_embedder(settings: Settings | None = None) -> DoubaoEmbedder:
    """嵌入的唯一构造点。本地 BGE 已移除，只剩 Doubao。"""

    return DoubaoEmbedder(settings or get_settings())

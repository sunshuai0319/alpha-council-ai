from collections.abc import Sequence
from typing import Any

from app.config import Settings, get_settings


class BGEEmbedder:
    """Lazy local BGE-M3 dense embedding adapter."""

    def __init__(self, settings: Settings | None = None, model: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._model = model

    @property
    def model(self) -> Any:
        if self._model is None:
            if not self.settings.embedding_model_path:
                raise RuntimeError("EMBEDDING_MODEL_PATH is not configured")
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.settings.embedding_model_path)
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        if len(texts) == 1 and getattr(vectors, "ndim", 2) == 1:
            return [vectors.tolist()]
        return [vector.tolist() for vector in vectors]


class BGEReranker:
    """Lazy local BGE-Reranker-v2-M3 cross-encoder adapter."""

    def __init__(self, settings: Settings | None = None, tokenizer: Any | None = None, model: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._tokenizer = tokenizer
        self._model = model

    def _load(self) -> tuple[Any, Any]:
        if self._tokenizer is None or self._model is None:
            if not self.settings.reranker_model_path:
                raise RuntimeError("RERANKER_MODEL_PATH is not configured")
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            self._tokenizer = self._tokenizer or AutoTokenizer.from_pretrained(
                self.settings.reranker_model_path
            )
            self._model = self._model or AutoModelForSequenceClassification.from_pretrained(
                self.settings.reranker_model_path
            )
            self._model.eval()
        return self._tokenizer, self._model

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        tokenizer, model = self._load()
        import torch

        inputs = tokenizer(
            [query] * len(documents),
            list(documents),
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        with torch.no_grad():
            logits = model(**inputs).logits.reshape(-1)
        return [float(score) for score in logits.tolist()]

import json
from dataclasses import dataclass
from typing import Any, Self

import httpx
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.processing.documents import DocumentInput, ProcessedDocument, prepare_document


class DocumentSummary(BaseModel):
    summary: str = Field(min_length=1)
    event_type: str
    assets: list[str] = Field(default_factory=list)
    direction: str
    impact_horizon: str
    confidence: float = Field(ge=0, le=1)


class SummaryError(RuntimeError):
    """Raised when Ark cannot produce a validated document summary."""


class ArkSummaryClient:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client or httpx.Client(timeout=self.settings.ark_timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def summarize(self, document: ProcessedDocument) -> DocumentSummary:
        prompt = (
            "Summarize this market document for a crypto trading research system. "
            "Return JSON only with summary, event_type, assets, direction, impact_horizon, "
            "and confidence (0 to 1). Do not provide trade orders or financial advice.\n\n"
            f"Title: {document.title}\n"
            f"Detected assets: {', '.join(document.assets) or 'none'}\n"
            f"Text: {document.cleaned_text}"
        )
        response = self._client.post(
            f"{self.settings.ark_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.ark_api_key}"},
            json={
                "model": self.settings.ark_model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You are a structured market-document analyst."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        try:
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            return DocumentSummary.model_validate(json.loads(_strip_json_fence(str(content))))
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise SummaryError(f"Ark summary failed: {exc}") from exc


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return stripped


@dataclass(frozen=True)
class DocumentProcessingResult:
    document: ProcessedDocument
    summary: DocumentSummary | None
    status: str
    error: str | None = None


class DocumentProcessor:
    def __init__(self, summary_client: Any) -> None:
        self.summary_client = summary_client

    def process(self, document: DocumentInput) -> "DocumentProcessingResult":
        prepared = prepare_document(document)
        try:
            summary = self.summary_client.summarize(prepared)
        except Exception as exc:  # noqa: BLE001 - retain document for retry after NLP failure
            return DocumentProcessingResult(document=prepared, summary=None, status="FAILED", error=str(exc))
        return DocumentProcessingResult(document=prepared, summary=summary, status="PROCESSED")

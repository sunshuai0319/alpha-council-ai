import json
from dataclasses import dataclass
from typing import Any, Self

import httpx
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, get_settings
from app.processing.documents import (
    DocumentInput,
    ProcessedDocument,
    merge_assets,
    normalize_event_type,
    normalize_impact_horizon,
    prepare_document,
)


class DocumentSummary(BaseModel):
    summary: str = Field(min_length=1)
    event_type: str
    assets: list[str] = Field(default_factory=list)
    direction: str
    impact_horizon: str
    confidence: float = Field(ge=0, le=1)

    @field_validator("direction", "event_type", "impact_horizon", mode="before")
    @classmethod
    def _flatten_structured_labels(cls, value: Any) -> Any:
        """模型有时把标签返回成结构而不是字符串 —— 拍平，别拒绝。

        线上实测：``direction`` 回过 ``{'BTC': 'outflows/negative'}``，pydantic
        直接拒绝整个 summary，那篇文档因此永远进不了库（库里 attempts 一路涨）。

        这些标签随后都会被 ``normalize_*`` 收敛到有界分类，所以把结构拍平不会
        丢信息 —— **拒绝才会**。拍平后的 ``"BTC outflows/negative"`` 会被
        ``normalize_direction`` 正确识别成 BEARISH。
        """

        if isinstance(value, dict):
            return " ".join(f"{key} {item}" for key, item in value.items())
        if isinstance(value, (list, tuple, set)):
            return " ".join(str(item) for item in value)
        return value


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
    def __init__(self, summary_client: Any, known_assets: tuple[str, ...] = ()) -> None:
        self.summary_client = summary_client
        self.known_assets = known_assets

    def process(self, document: DocumentInput) -> "DocumentProcessingResult":
        prepared = prepare_document(document, known_assets=self.known_assets)
        try:
            summary = self.summary_client.summarize(prepared)
        except Exception as exc:  # noqa: BLE001 - retain document for retry after NLP failure
            return DocumentProcessingResult(document=prepared, summary=None, status="FAILED", error=str(exc))
        summary = summary.model_copy(
            update={
                "assets": list(merge_assets(prepared.assets, summary.assets, self.known_assets)),
                "event_type": normalize_event_type(summary.event_type or prepared.event_type),
                "impact_horizon": normalize_impact_horizon(summary.impact_horizon),
            }
        )
        return DocumentProcessingResult(document=prepared, summary=summary, status="PROCESSED")

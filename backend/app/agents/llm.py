import json
from typing import Any, Self

import httpx

from app.config import Settings, get_settings


class AgentLLMError(RuntimeError):
    """Raised when the committee model cannot return a usable response."""


class ArkChatClient:
    """Minimal OpenAI-compatible Ark client for agent prompts."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None) -> None:
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

    def complete_json(self, prompt: str) -> str:
        response = self._client.post(
            f"{self.settings.ark_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.ark_api_key}"},
            json={
                "model": self.settings.ark_model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You are a cautious structured crypto market analyst."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        try:
            response.raise_for_status()
            content: Any = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            return _strip_json_fence(str(content))
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise AgentLLMError(f"Ark agent completion failed: {exc}") from exc


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return stripped


def parse_json_response(response: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    payload = json.loads(_strip_json_fence(response))
    if not isinstance(payload, dict):
        raise TypeError("agent response must be a JSON object")
    return payload

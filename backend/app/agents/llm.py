import json
import time
from typing import Any, Self

import httpx

from app.config import Settings, get_settings


class AgentLLMError(RuntimeError):
    """Raised when the committee model cannot return a usable response."""


#: 只对瞬时故障重试。5xx 是服务端暂时过载；4xx 是请求配置错误，重试结果一样，
#: 直接失败并保留异常细节（401 能定位 key/passphrase/secret 问题）。
def _is_retryable_http_status(status: int) -> bool:
    return status >= 500


class ArkChatClient:
    """Minimal OpenAI-compatible Ark client for agent prompts.

    对瞬时故障（读超时 / 网络抖动 / 5xx）重试 ``ark_retry_attempts`` 次，指数退避。
    LLM 生成是幂等无副作用的，重发一次通常就成功 —— 实测三个 agent 并行时偶发
    ReadTimeout，整个分析却因此退化成 neutral。4xx 与解析失败不重试。
    """

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
        attempts = self.settings.ark_retry_attempts + 1
        backoff = self.settings.ark_retry_backoff_seconds
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._complete_once(prompt)
            except _RetryableError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(backoff * (2**attempt))
        raise AgentLLMError(
            f"Ark agent completion failed after {attempts} attempts: {last_error}"
        ) from last_error

    def _complete_once(self, prompt: str) -> str:
        try:
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
            response.raise_for_status()
            content: Any = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            return _strip_json_fence(str(content))
        except httpx.HTTPStatusError as exc:
            if _is_retryable_http_status(exc.response.status_code):
                raise _RetryableError(f"Ark HTTP {exc.response.status_code}") from exc
            raise AgentLLMError(f"Ark agent completion failed: {exc}") from exc
        except httpx.TimeoutException as exc:
            # 读 / 连接 / 写超时都是瞬时故障，值得重试。
            raise _RetryableError(f"Ark {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise _RetryableError(f"Ark {type(exc).__name__}") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # 200 之后解析失败：重发会得到同样的坏结果，重试无意义。
            raise AgentLLMError(f"Ark agent completion failed: {exc}") from exc


class _RetryableError(RuntimeError):
    """内部标记：瞬时故障，允许重试。不对外暴露。"""


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

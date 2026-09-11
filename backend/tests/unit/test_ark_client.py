"""ArkChatClient 的重试策略：瞬时故障要重试，配置类错误不重试。

实测触发过：三个 agent 并行调同一 Ark LLM，其中一个请求读超时
（ReadTimeout，60s 内没读完），整条分析退化成 neutral —— 而同一个 prompt
重发一次往往就成功了。LLM 生成是幂等无副作用的，重试是安全的。
"""

import httpx
import pytest

from app.agents.llm import AgentLLMError, ArkChatClient
from app.config import Settings


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        MILVUS_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
        ARK_BASE_URL="https://ark.test/api/v3",
    )


def _client(handler, settings: Settings | None = None) -> ArkChatClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://ark.test/api/v3", transport=transport)
    return ArkChatClient(settings or _settings(), client=http_client)


def _json_response(content: str = '{"ok": true}') -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_retries_a_read_timeout_and_recovers() -> None:
    """读超时是瞬时故障：重试一次应能拿到结果。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("first read timed out")
        return _json_response()

    client = _client(handler)
    assert client.complete_json("prompt") == '{"ok": true}'
    assert calls["n"] == 2, "超时后应重试一次并成功"


def test_gives_up_after_all_attempts() -> None:
    """持续超时必须在耗尽重试次数后抛错，不能无限重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        raise httpx.ReadTimeout("always times out")

    client = _client(handler)
    with pytest.raises(AgentLLMError):
        client.complete_json("prompt")
    # 默认 ark_retry_attempts=2 → 共 3 次尝试
    assert calls["n"] == 3


def test_retries_http_5xx_but_not_4xx() -> None:
    """5xx 是服务端瞬时故障可重试；4xx 是配置错误，重试无意义。"""
    server_calls = {"n": 0}
    client_calls = {"n": 0}

    def server_error(request: httpx.Request) -> httpx.Response:
        del request
        server_calls["n"] += 1
        if server_calls["n"] == 1:
            return httpx.Response(503, json={"error": "overloaded"})
        return _json_response()

    def client_error(request: httpx.Request) -> httpx.Response:
        del request
        client_calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    ok_client = _client(server_error)
    assert ok_client.complete_json("prompt") == '{"ok": true}'
    assert server_calls["n"] == 2

    bad_client = _client(client_error)
    with pytest.raises(AgentLLMError):
        bad_client.complete_json("prompt")
    assert client_calls["n"] == 1, "4xx 不应重试"


def test_does_not_retry_on_parse_failure() -> None:
    """HTTP body 不是合法 JSON（response.json 抛错）：重发会得到同样的坏结果，不该重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        return httpx.Response(200, content=b"this is not json at all")

    client = _client(handler)
    with pytest.raises(AgentLLMError):
        client.complete_json("prompt")
    assert calls["n"] == 1, "解析失败发生在 200 之后，重试无意义"


def test_retry_count_is_configured_through_settings() -> None:
    """重试次数可配；设为 0 时一次超时就抛错。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        raise httpx.ReadTimeout("times out")

    settings = _settings().model_copy(update={"ark_retry_attempts": 0})
    client = _client(handler, settings)
    with pytest.raises(AgentLLMError):
        client.complete_json("prompt")
    assert calls["n"] == 1

import httpx

USER_AGENT = "alpha-council-ai/0.1 (+https://example.invalid)"
TIMEOUT_SECONDS = 20


class ConditionalFetcher:
    """带 ETag / Last-Modified 的 GET。

    命中 304 时返回 ``None``，调用方据此跳过解析：feed 没变就不必重复下载和解析，
    这是让每轮重抓几乎零成本的前提。校验值按 URL 保存在进程内存里，worker 重启
    后首次抓取拿不到 304，但代价只是一次全量下载。
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        user_agent: str = USER_AGENT,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._user_agent = user_agent
        self._timeout = timeout
        self._validators: dict[str, tuple[str | None, str | None]] = {}

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def __call__(self, url: str) -> bytes | None:
        etag, last_modified = self._validators.get(url, (None, None))
        headers = {"User-Agent": self._user_agent}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        response = self._get(url, headers)
        if response.status_code == httpx.codes.NOT_MODIFIED:
            return None
        response.raise_for_status()
        self._validators[url] = (
            response.headers.get("etag"),
            response.headers.get("last-modified"),
        )
        return response.content

    def _get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        if self._client is not None:
            return self._client.get(url, headers=headers, follow_redirects=True)
        return httpx.get(url, headers=headers, timeout=self._timeout, follow_redirects=True)

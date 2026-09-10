from types import SimpleNamespace

import httpx

from app.collectors.fetching import ConditionalFetcher
from app.collectors.macro import MacroCollector
from app.collectors.rss import RSSCollector, canonicalize_url


def test_rss_canonical_url_and_content_hash_deduplicate_items() -> None:
    entry = {
        "id": "https://news.example.com/article/1",
        "link": "https://news.example.com/article/1?utm_source=rss",
        "title": "Bitcoin moves",
        "summary": "<p>BTC gained.</p>",
    }

    def fetcher(_: str) -> bytes:
        return b"feed"

    def parser(_: bytes) -> SimpleNamespace:
        return SimpleNamespace(entries=[entry])

    collector = RSSCollector(
        sources={"one": "https://feed.example.com/one", "two": "https://feed.example.com/two"},
        fetcher=fetcher,
        parser=parser,
    )
    result = collector.collect()

    assert canonicalize_url(entry["link"]) == entry["id"]
    assert len(result.items) == 1
    assert result.items[0].summary == "BTC gained."
    assert result.ok


def test_rss_failure_is_recorded_without_losing_other_sources() -> None:
    def fetcher(url: str) -> bytes:
        if url.endswith("bad"):
            raise RuntimeError("feed unavailable")
        return b"feed"

    def parser(_: bytes) -> SimpleNamespace:
        return SimpleNamespace(
            entries=[
                {
                    "link": "https://news.example.com/good",
                    "title": "Good source",
                    "summary": "summary",
                }
            ]
        )

    result = RSSCollector(
        sources={"good": "https://feed.example.com/good", "bad": "https://feed.example.com/bad"},
        fetcher=fetcher,
        parser=parser,
    ).collect()
    assert len(result.items) == 1
    assert result.partial
    assert "bad" in result.errors[0]


def test_conditional_fetcher_sends_validators_and_reports_unchanged() -> None:
    """feed 没变就不该重复下载：带上 ETag 后命中 304。"""
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        if len(seen) == 1:
            return httpx.Response(200, content=b"feed", headers={"ETag": '"v1"'})
        return httpx.Response(304)

    fetcher = ConditionalFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert fetcher("https://example.com/feed") == b"feed"
    assert fetcher("https://example.com/feed") is None
    assert seen[0].get("If-None-Match") is None
    assert seen[1]["If-None-Match"] == '"v1"'


def test_conditional_fetcher_returns_new_content_when_the_feed_changed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"new", headers={"ETag": '"v2"'})

    fetcher = ConditionalFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert fetcher("https://example.com/feed") == b"new"


def test_rss_skips_parsing_when_the_feed_is_unchanged() -> None:
    parsed: list[object] = []

    collector = RSSCollector(
        fetcher=lambda url: None,  # 304 Not Modified
        parser=lambda payload: parsed.append(payload) or SimpleNamespace(entries=[]),
    )

    result = collector.collect()

    assert parsed == []  # 未变更就不解析
    assert result.errors == []
    assert set(result.succeeded) == {"coindesk", "cointelegraph", "bitcoin-magazine"}


def test_fred_csv_is_normalized_and_missing_values_are_preserved() -> None:
    csv_payload = "observation_date,value\n2026-01-01,4.33\n2026-02-01,.\n"

    def fetcher(_: str) -> str:
        return csv_payload

    result = MacroCollector(fetcher=fetcher).collect_fred(("FEDFUNDS",), limit=2)
    assert len(result.items) == 2
    assert result.items[0].series_id == "FEDFUNDS"
    assert result.items[0].value == 4.33
    assert result.items[1].value is None
    assert result.items[1].observation_date.isoformat() == "2026-02-01"


def test_macro_source_failure_isolated_per_series() -> None:
    def fetcher(url: str) -> str:
        if "BAD" in url:
            raise RuntimeError("series unavailable")
        return "DATE,VALUE\n2026-01-01,1.0\n"

    result = MacroCollector(fetcher=fetcher).collect_fred(("GOOD", "BAD"), limit=1)
    assert len(result.items) == 1
    assert result.items[0].series_id == "GOOD"
    assert len(result.errors) == 1
